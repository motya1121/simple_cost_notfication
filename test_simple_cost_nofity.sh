#!/bin/zsh

source .env

export Region=${Region}
export PROFILE_NAME=${PROFILE_NAME}
export SENDER_EMAIL=${SENDER_EMAIL}
export PROJECT_DATA_PARAMETER_NAME=${PROJECT_DATA_PARAMETER_NAME}

python lambda/simple_cost_nofity/lambda_function.py