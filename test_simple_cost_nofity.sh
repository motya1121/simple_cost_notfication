#!/bin/zsh

source .env

export Region=${Region}
export PROFILE_NAME=${PROFILE_NAME}
export SENDER_EMAIL=${SENDER_EMAIL}
export PROJECT_DATA_PARAMETER_NAME=${PROJECT_DATA_PARAMETER_NAME}
export RATE_VALUE=${RATE_VALUE}
export IS_DEBUG=${IS_DEBUG}
export IS_SEND_DEBUG_MAIL=${IS_SEND_DEBUG_MAIL}
export SECRET_PARAMETER_NAME=${SECRET_PARAMETER_NAME}

python lambda/simple_cost_nofity/lambda_function.py