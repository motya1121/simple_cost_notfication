#!/bin/zsh

source .env

# --- メイン処理 ---
echo "処理を開始します..."

for LAMBDA_FUNC_NAME in "${LAMBDA_FUNC_NAMEs[@]}"; do
    echo "--------------------"
    echo "現在の処理対象: ${LAMBDA_FUNC_NAME}"

    # create vm cide
    cd lambda/${LAMBDA_FUNC_NAME}
    pip install -r requirements.txt -t .
    rm -r *-info
    zip -q -r ../${LAMBDA_FUNC_NAME} ./*
    rm -r bin
    rm -r certifi
    rm -r charset_normalizer
    rm -r idna
    rm -r requests
    rm -r urllib3
    rm -r azure
    rm -r cffi
    rm -r cryptography
    rm -r isodate
    rm -r jwt
    rm -r msal
    rm -r msal_extensions
    rm -r pycparser
    rm _cffi_backend.cpython-312-x86_64-linux-gnu.so
    rm six.py
    rm typing_extensions.py
    cd ..

    aws lambda update-function-code --function-name ${LAMBDA_FUNC_NAME} --zip-file fileb://${LAMBDA_FUNC_NAME}.zip --region ${Region} --output text --profile ${PROFILE_NAME}

    rm ${LAMBDA_FUNC_NAME}.zip
    cd ..

done

echo "--------------------"
echo "全ての処理が完了しました。"

exit 0


