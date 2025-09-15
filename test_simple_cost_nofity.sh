#!/bin/zsh

source .env

export Region=${Region}
export PROFILE_NAME=${PROFILE_NAME}
export SENDER_EMAIL=${SENDER_EMAIL}
export PROJECT_DATA_PARAMETER_NAME=${PROJECT_DATA_PARAMETER_NAME}
export RATE_VALUE=${RATE_VALUE}
export AZ_CLIENT_ID=${AZ_CLIENT_ID}
export AZ_CLIENT_SECRET=${AZ_CLIENT_SECRET}
export AZ_TENANT_ID=${AZ_TENANT_ID}
export AZ_SUB_ID=${AZ_SUB_ID}

python lambda/simple_cost_nofity/lambda_function.py