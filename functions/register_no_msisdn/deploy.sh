#!/usr/bin/env bash

set -e

if [[ -z ${REGION} || -z ${FUNCTIONS_BUCKET}  ]]; then
    echo
    echo "ERROR! One of variables: [\"REGION\", \"FUNCTIONS_BUCKET\"] is not set. Exiting!"
    echo
    exit 1
fi

gcloud functions deploy register_no_msisdn \
    --region=${REGION} \
    --source=./ \
    --runtime=python37 \
    --stage-bucket=${FUNCTIONS_BUCKET} \
    --trigger-http --allow-unauthenticated \
    --entry-point register_no_msisdn