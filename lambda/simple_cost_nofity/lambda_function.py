import base64
import json
import logging
import os
from datetime import datetime as dt
from datetime import timezone

from azure.identity import ClientSecretCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryAggregation,
    QueryDataset,
    QueryDefinition,
    QueryGrouping,
)
from boto3.session import Session

logger = logging.getLogger()
logger.setLevel(logging.INFO)

region_name = os.environ["Region"]
PROFILE_NAME = os.environ.get("PROFILE_NAME", "")
if PROFILE_NAME == "":
    session = Session(region_name=region_name)
else:
    session = Session(region_name=region_name, profile_name=PROFILE_NAME)
ce_client = session.client("ce")
ses_client = session.client("ses")
ssm_client = session.client("ssm")
org_client = session.client("organizations")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
PROJECT_DATA_PARAMETER_NAME = os.environ.get("PROJECT_DATA_PARAMETER_NAME")
SECRET_PARAMETER_NAME = os.environ.get("SECRET_PARAMETER_NAME")
SUBJECT = os.environ.get("SUBJECT", "simple cost notification")
RATE = float(os.environ.get("RATE_VALUE"))


def get_ssm_parameter():
    response = ssm_client.get_parameter(Name=PROJECT_DATA_PARAMETER_NAME)
    project_data = json.loads(response["Parameter"]["Value"])

    response = ssm_client.get_parameter(Name=SECRET_PARAMETER_NAME, WithDecryption=True)
    az_credentials = json.loads(response["Parameter"]["Value"])

    return project_data, az_credentials


def get_aws_cost_and_usage():
    # aws ce get-cost-and-usage --time-period Start=2025-01-01,End=2025-01-31 --granularity DAILY --metrics "UnblendedCost" --group-by Type=DIMENSION,Key=SERVICE Type=DIMENSION,Key=LINKED_ACCOUNT --profile maina

    today = dt.now(timezone.utc)
    current_month = today.month
    current_year = today.year
    next_month = current_month + 1
    next_year = current_year
    if next_month == 13:
        next_month = 1
        next_year = current_year + 1
    period_start = f"{current_year}-{current_month:02}-01"
    period_end = f"{next_year}-{next_month:02}-01"
    params = {
        "TimePeriod": {"Start": period_start, "End": period_end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost", "NetUnblendedCost"],
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
        ],
    }

    all_results = []
    next_page_token = None

    # ループで全ページを取得
    while True:
        if next_page_token:
            params["NextPageToken"] = next_page_token

        response = ce_client.get_cost_and_usage(**params)
        all_results.extend(response["ResultsByTime"])
        next_page_token = response.get("NextPageToken")
        if not next_page_token:
            break

    return all_results


def get_all_account_names():
    """
    AWS OrganizationsからすべてのアカウントIDとアカウント名のマッピングを取得します。
    """
    account_map = {}
    try:
        paginator = org_client.get_paginator("list_accounts")
        for page in paginator.paginate():
            for account in page["Accounts"]:
                account_map[account["Id"]] = account["Name"]
    except Exception as e:
        logger.error(f"Failed to list accounts from Organizations: {e}")
    return account_map


def sort_out_aws_cost(cost_datas, project_data):
    DEFAULT_PROJECT = project_data["default_project"]
    _project_data = project_data["project_data"]
    cost_results = {}
    cost_results_account = {}
    for project_name in _project_data.keys():
        cost_results[project_name] = {}
        cost_results_account[project_name] = {}

    for cost_data in cost_datas:
        for group in cost_data["Groups"]:
            service = group["Keys"][0]
            account_id = group["Keys"][1]
            try:
                usd = float(group["Metrics"]["NetUnblendedCost"]["Amount"])
            except KeyError:
                usd = float(group["Metrics"]["UnblendedCost"]["Amount"])

            project_flag = False
            for project_name, cost_result in cost_results.items():
                if account_id in _project_data[project_name]["AccountID"]:
                    # per service
                    if service in cost_result.keys():
                        cost_result[service] += usd
                    else:
                        cost_result[service] = usd

                    # per account id
                    if account_id in cost_results_account[project_name].keys():
                        cost_results_account[project_name][account_id] += usd
                    else:
                        cost_results_account[project_name][account_id] = usd

                    project_flag = True

            if project_flag is False:
                # per service
                if service in cost_results[DEFAULT_PROJECT].keys():
                    cost_results[DEFAULT_PROJECT][service] += usd
                else:
                    cost_results[DEFAULT_PROJECT][service] = usd

                # per account
                if account_id in cost_results_account[DEFAULT_PROJECT].keys():
                    cost_results_account[DEFAULT_PROJECT][account_id] += usd
                else:
                    cost_results_account[DEFAULT_PROJECT][account_id] = usd

    return cost_results, cost_results_account


def get_azure_cost_and_usage(az_credentials: list):
    # 環境変数から認証情報を取得

    for az_credential in az_credentials:
        try:
            credential = ClientSecretCredential(
                tenant_id=az_credential["az_tenant_id"],
                client_id=az_credential["az_client_id"],
                client_secret=az_credential["az_client_secret"],
            )
        except KeyError:
            print(
                "エラー: 必要な環境変数（AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET）が設定されていません。"
            )
            return None

        # Cost Management クライアントの初期化
        cost_management_client = CostManagementClient(credential)

        # クエリのスコープ（対象範囲）を設定
        scope = f"/subscriptions/{az_credential['az_subscription_id']}"

        # Cost Management APIに投げるクエリを定義
        query_definition = QueryDefinition(
            type="ActualCost",  # 実績コストを取得
            timeframe="MonthToDate",  # 期間を「当月初日から本日まで」に設定
            dataset=QueryDataset(
                granularity="Daily",  # 日単位で集計
                aggregation={
                    "totalCost": QueryAggregation(name="Cost", function="Sum")
                },
                grouping=[
                    QueryGrouping(type="Dimension", name="SubscriptionId"),
                    QueryGrouping(type="Dimension", name="SubscriptionName"),
                    QueryGrouping(type="Dimension", name="ServiceName"),
                ],
            ),
        )

        try:
            # APIを実行してコスト情報を取得
            result = cost_management_client.query.usage(scope, query_definition)

            # 結果を整形
            # columns = [col.name for col in result.columns]
            data = result.rows

        except Exception as e:
            print(f"エラーが発生しました: {e}")
            return None

        return data


def sort_out_azure_cost(cost_datas, project_data):
    DEFAULT_PROJECT = project_data["default_project"]
    _project_data = project_data["project_data"]
    cost_results = {}
    cost_results_account = {}
    for project_name in _project_data.keys():
        cost_results[project_name] = {}
        cost_results_account[project_name] = {}

    for cost_data in cost_datas:
        cost_jpy = cost_data[0]
        sub_id = cost_data[2]
        service = cost_data[4]

        project_flag = False
        for project_name, cost_result in cost_results.items():
            if sub_id in _project_data[project_name]["SubscriptionID"]:
                # per service
                if service in cost_result.keys():
                    cost_result[service] += cost_jpy
                else:
                    cost_result[service] = cost_jpy

                # per account id
                if sub_id in cost_results_account[project_name].keys():
                    cost_results_account[project_name][sub_id] += cost_jpy
                else:
                    cost_results_account[project_name][sub_id] = cost_jpy
                project_flag = True

        if project_flag is False:
            # per service
            if service in cost_results[DEFAULT_PROJECT].keys():
                cost_results[DEFAULT_PROJECT][service] += cost_jpy
            else:
                cost_results[DEFAULT_PROJECT][service] = cost_jpy

            # per account
            if sub_id in cost_results_account[DEFAULT_PROJECT].keys():
                cost_results_account[DEFAULT_PROJECT][sub_id] += cost_jpy
            else:
                cost_results_account[DEFAULT_PROJECT][sub_id] = cost_jpy

    return cost_results, cost_results_account


def create_email_html(sort_cost_data, budget_yen, account_names, az_credentials):
    # AWS
    aws_total_cost = sum(sort_cost_data["AWS"].values()) * RATE
    azure_total_cost = sum(sort_cost_data["Azure"].values())

    all_cost_data = {}
    for service, cost in sort_cost_data["AWS"].items():
        all_cost_data[service] = cost * RATE
    all_cost_data.update(sort_cost_data["Azure"])

    # top 10
    sorted_data = sorted(all_cost_data.items(), key=lambda item: item[1], reverse=True)
    top_10 = sorted_data[:10]
    tbody = ""
    i = 1
    for item, value in top_10:
        tbody += f"""<tr>
                <td>{i}</td>
                <td>{item}</td>
                <td style='text-align: right;'>{value:,.2f} 円</td>
            </tr>"""
        i = i + 1

    # 数値の合計値を取得
    total_value = sum(all_cost_data.values())

    # 今月の予測
    today = dt.now(timezone.utc)
    cost_per_day = total_value / today.day
    predict_month_cost = cost_per_day * 31

    # 予算との差分
    diff_budget_predict = budget_yen - predict_month_cost

    # アカウントごと
    per_account_data = []
    per_account = ""
    for account_id, cost_data in sort_cost_data["AWS_Accounts"].items():
        account_name = account_names.get(account_id, account_id)
        per_account_data.append([account_id, account_name, cost_data * RATE])
    for account_id, cost_data in sort_cost_data["Azure_Accounts"].items():
        account_name = "-"
        for az_credential in az_credentials:
            if az_credential["az_subscription_id"] == account_id:
                account_name = az_credential["az_subscription_name"]
                break
        per_account_data.append([account_id, account_name, cost_data])
        per_account_data = sorted(
            per_account_data, key=lambda item: item[2], reverse=True
        )
    for account_id, account_name, cost_data in per_account_data:
        per_account += f"<tr><td>{account_id}</td><td>{account_name}</td><td style='text-align: right;'>{cost_data:,.0f} 円</td></tr>"

    cost_report = f"""<div>
            <h2>これまでの利用料金</h2>
            <p>{total_value:,.0f} 円</p>
            <h2>今月の料金予測(31日で計算)</h2>
            <p>{predict_month_cost:,.0f} 円</p>
            <h2>予算との差分</h2>
            <p>予算({budget_yen:,})-予測({predict_month_cost:,.0f})= {diff_budget_predict:,.0f} 円</p>
            <h2>予算の利用割合</h2>
            <p>{round(total_value*100/budget_yen)} %</p>
        </div>

        <div>
            <h2>クラウドごとの利用料金</h2>
            <table>
                <thead>
                    <tr>
                        <th>クラウドプロバイダ</th>
                        <th>利用料</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>AWS</td>
                        <td style='text-align: right;'>{aws_total_cost:,.0f} 円</td>
                    </tr>
                    <tr>
                        <td>Azure</td>
                        <td style='text-align: right;'>{azure_total_cost:,.0f} 円</td>
                    </tr>
                </tbody>
            </table>
        </div>

        <div>
            <h2>アカウントごとの利用料金</h2>
            <table>
                <thead>
                    <tr>
                        <th>アカウントID</th>
                        <th>アカウント名</th>
                        <th>利用料</th>
                    </tr>
                </thead>
                <tbody>
                    {per_account}
                </tbody>
            </table>
        </div>

        <div>
            <h2>利用料が高いリソース トップ10</h2>
            <table>
                <thead>
                    <tr>
                        <th>順位</th>
                        <th>リソース名</th>
                        <th>利用料</th>
                    </tr>
                </thead>
                <tbody>
                    {tbody}
                </tbody>
            </table>
        </div>"""
    return cost_report


def send_email(project, cost_report):
    charset = "UTF-8"

    today = dt.now(timezone.utc)
    if today.month in [1, 3, 5, 7, 8, 10, 12]:
        percent = int(today.day) * 100 / 31
    elif today.month == 2:
        percent = int(today.day) * 100 / 28
    else:
        percent = int(today.day) * 100 / 30

    body_html = f"""<html>
    <body>
        <h1>{SUBJECT} - {project}(今月の{round(percent)}%終了)</h1>
        {cost_report}
    </body>
    </html>"""

    try:
        ses_client.send_email(
            Source=SENDER_EMAIL,
            Destination={
                "ToAddresses": [
                    SENDER_EMAIL,
                ],
            },
            Message={
                "Subject": {"Data": f"{SUBJECT} - {project}", "Charset": charset},
                "Body": {"Html": {"Data": body_html, "Charset": charset}},
            },
        )
        print("メールを送信しました。")
    except Exception as e:
        error_message = f"メール送信中にエラーが発生しました: {str(e)}"
        print(f"Error: {error_message}")


def lambda_handler(event, context):
    project_data, az_credentials = get_ssm_parameter()

    # AWS
    account_names = get_all_account_names()
    aws_cost_datas = get_aws_cost_and_usage()
    sort_aws_results, sort_aws_account_results = sort_out_aws_cost(
        aws_cost_datas, project_data
    )

    # Azure
    azure_cost_data = get_azure_cost_and_usage(az_credentials=az_credentials)
    sort_azure_results, sort_azure_account_results = sort_out_azure_cost(
        azure_cost_data, project_data
    )

    # join service
    sort_results = {}
    for project, sort_result in sort_aws_results.items():
        sort_results[project] = {}
        sort_results[project]["AWS"] = sort_result
    for project, sort_result in sort_azure_results.items():
        if sort_results.get(project, None) is not None:
            sort_results[project]["Azure"] = sort_result
        else:
            sort_results[project]["Azure"] = sort_result

    # join account
    for project, sort_result in sort_aws_account_results.items():
        sort_results[project]["AWS_Accounts"] = sort_result
    for project, sort_result in sort_azure_account_results.items():
        sort_results[project]["Azure_Accounts"] = sort_result

    for project, sort_result in sort_results.items():
        cost_report = create_email_html(
            sort_result,
            project_data["project_data"][project]["budget_yen"],
            account_names,
            az_credentials,
        )
        send_email(project, cost_report)


if __name__ == "__main__":
    lambda_handler({}, {})
