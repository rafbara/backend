import json
import os
import random
import logging
import secrets
import string
import re
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import pytz
from flask import jsonify, Request, Response, current_app
from google.cloud import datastore
from google.cloud import pubsub_v1

from rate_limit import limit_requests

current_app.config["JSON_AS_ASCII"] = False
PROJECT_ID = os.environ["GCP_PROJECT"]
PUBSUB_SEND_REGISTER_SMS_TOPIC = os.environ["PUBSUB_SEND_REGISTER_SMS_TOPIC"]
STAGE = os.environ["STAGE"]

INVALID_REGS_PER_IP_LIMIT = 10
INVALID_REGS_PER_MSISDN_LIMIT = 4
SEND_SMS_LIMIT_PER_MINUTE = 1
SEND_SMS_LIMIT_PER_HOUR = 2
SEND_SMS_LIMIT_PER_24_HOURS = 5
CODE_CHARACTERS = string.digits
DATA_STORE_REGISTRATION_KIND = "Registrations"
REGISTRATION_STATUS_PENDING = "pending"
REGISTRATION_STATUS_INCORRECT = "incorrect"

MESSAGE_INVALID_PHONE_NUMBER = "invalid_phone_number"
MESSAGE_REGISTRATION_NOT_AVAILABLE = "registration_not_available"

with open("messages.json") as file:
    MESSAGES = json.load(file)

datastore_client = datastore.Client()
publisher = pubsub_v1.PublisherClient()


@limit_requests()
def register(request):
    is_request_valid, response = _is_request_valid(request)
    if not is_request_valid:
        return response

    request_data = request.get_json()
    msisdn = request_data["msisdn"]
    ip = request.headers.get("X-Forwarded-For").split(",")[-1]

    lang = request_data["lang"]
    code = _get_pending_registration_code(msisdn) or "".join(random.choice(CODE_CHARACTERS) for _ in range(6))
    registration_id = secrets.token_hex(32)
    date = datetime.now(tz=pytz.utc)

    _save_to_datastore(code, msisdn, date, registration_id, ip)

    response = {"status": "ok", "registration_id": registration_id}

    send_sms = request_data.get("send_sms", True)
    if STAGE == "DEVELOPMENT" and not send_sms:
        response["code"] = code
    elif _should_send_sms(msisdn):
        _publish_to_send_register_sms_topic(msisdn, registration_id, code, lang)

    return jsonify(response)


def _is_request_valid(request: Request) -> Tuple[bool, Optional[Tuple[Response, int]]]:
    if request.method != "POST":
        return False, (jsonify({"status": "failed", "message": "Invalid method"}), 405)

    if not request.is_json:
        return False, (jsonify({"status": "failed", "message": "Invalid data"}), 422)

    request_data = request.get_json()

    if not _is_language_valid(request_data):
        return False, (jsonify({"status": "failed", "message": "Set lang parameter to pl or en"}), 422)

    lang = request_data["lang"]

    if "msisdn" not in request_data or not _check_phone_number(request_data["msisdn"]):
        return False, (jsonify({"status": "failed", "message": _get_message(MESSAGE_INVALID_PHONE_NUMBER, lang)}), 422)

    msisdn = request_data["msisdn"]

    if _is_too_many_requests_for("msisdn", msisdn, limit=INVALID_REGS_PER_MSISDN_LIMIT):
        return (
            False,
            (jsonify({"status": "failed", "message": _get_message(MESSAGE_REGISTRATION_NOT_AVAILABLE, lang)}), 429),
        )

    return True, None


def _is_language_valid(request_data: dict) -> bool:
    languages_available = ("pl", "en")
    lang = request_data.get("lang")
    if lang not in languages_available:
        logging.warning(f"Invalid lang: {lang}")
        return False
    return True


def _get_message(message_code: str, lang: str) -> str:
    return MESSAGES[message_code][lang]


def _check_phone_number(msisdn: str):
    msisdn = re.sub("[^0-9,+]", "", msisdn)
    if re.match(r"^\+48[0-9]{9}$", msisdn):
        return True
    logging.warning(f"check_phone_number: invalid phone number: {msisdn}")
    return False


def _is_too_many_requests_for(field: str, value: str, limit: int) -> bool:
    registration_entities = _get_registration_entities(field, value, timedelta(hours=1), status=REGISTRATION_STATUS_PENDING)
    registration_entities += _get_registration_entities(field, value, timedelta(hours=1), status=REGISTRATION_STATUS_INCORRECT)

    if len(registration_entities) >= limit:
        logging.warning(f"_is_too_many_requests_for: {field}: {value}")
        return True

    return False


def _get_pending_registration_code(msisdn: str) -> Optional[str]:
    registration_entities = _get_registration_entities(
        "msisdn", msisdn, timedelta(minutes=10), status=REGISTRATION_STATUS_PENDING
    )

    if len(registration_entities) > 0:
        logging.info("_get_pending_registration_code: returning existing code")
        return registration_entities[0]["code"]

    return None


def _should_send_sms(msisdn: str) -> bool:
    registration_entities_last_minute = _get_registration_entities("msisdn", msisdn, timedelta(minutes=1))
    registration_entities_last_hour = _get_registration_entities("msisdn", msisdn, timedelta(hours=1))
    registration_entities_last_24_hours = _get_registration_entities("msisdn", msisdn, timedelta(days=1))

    if (
        len(registration_entities_last_minute) > SEND_SMS_LIMIT_PER_MINUTE
        or len(registration_entities_last_hour) > SEND_SMS_LIMIT_PER_HOUR
        or len(registration_entities_last_24_hours) > SEND_SMS_LIMIT_PER_24_HOURS
    ):
        logging.warning(f"_should_send_sms: resend sms request for msisdn: {msisdn}")
        return False

    return True


def _get_registration_entities(
    field: str, value: str, time_period: timedelta, status: Optional[str] = None
) -> List[datastore.Entity]:
    query = datastore_client.query(kind=DATA_STORE_REGISTRATION_KIND)
    query.add_filter(field, "=", value)
    if status:
        query.add_filter("status", "=", status)
    start_date = datetime.now(tz=pytz.utc) - time_period

    query.add_filter("date", ">", start_date)
    query.order = ["-date"]

    return list(query.fetch())


def _save_to_datastore(code: str, msisdn: str, date: datetime, registration_id: str, ip: str):
    key = datastore_client.key(DATA_STORE_REGISTRATION_KIND, f"{registration_id}")

    registration = datastore.Entity(key=key)
    registration.update(
        {
            "code": code,
            "msisdn": msisdn,
            "date": date,
            "registration_id": registration_id,
            "sms_send": False,
            "ip": ip,
            "status": REGISTRATION_STATUS_PENDING,
        }
    )

    datastore_client.put(registration)


def _publish_to_send_register_sms_topic(msisdn: str, registration_id: str, code: str, lang: str):
    topic_path = publisher.topic_path(PROJECT_ID, PUBSUB_SEND_REGISTER_SMS_TOPIC)
    data = {
        "registration_id": registration_id,
        "msisdn": msisdn,
        "code": code,
        "lang": lang,
    }
    publisher.publish(topic_path, json.dumps(data).encode("utf-8"))
