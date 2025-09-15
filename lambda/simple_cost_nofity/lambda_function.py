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
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
PROJECT_DATA_PARAMETER_NAME = os.environ.get("PROJECT_DATA_PARAMETER_NAME")
SUBJECT = os.environ.get("SUBJECT")
RATE = int(os.environ.get("RATE_VALUE"))


def get_ssm_parameter():
    response = ssm_client.get_parameter(Name=PROJECT_DATA_PARAMETER_NAME)
    parameter_value = json.loads(response["Parameter"]["Value"])
    return parameter_value


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

    response = ce_client.get_cost_and_usage(
        TimePeriod={"Start": period_start, "End": period_end},
        Granularity="DAILY",
        Metrics=["UnblendedCost", "NetUnblendedCost"],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
        ],
    )
    return response


def sort_out_aws_cost(cost_datas, project_data):
    DEFAULT_PROJECT = project_data["default_project"]
    _project_data = project_data["project_data"]
    cost_results = {}
    for project_name in _project_data.keys():
        cost_results[project_name] = {}

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
                    if service in cost_result.keys():
                        cost_result[service] += usd * RATE
                    else:
                        cost_result[service] = usd * RATE
                    project_flag = True

            if project_flag is False:
                if service in cost_results[DEFAULT_PROJECT].keys():
                    cost_results[DEFAULT_PROJECT][service] += usd * RATE
                else:
                    cost_results[DEFAULT_PROJECT][service] = usd * RATE

    return cost_results


def get_azure_cost_and_usage(subscription_id: str):
    # 環境変数から認証情報を取得
    try:
        credential = ClientSecretCredential(
            tenant_id=os.environ["AZ_TENANT_ID"],
            client_id=os.environ["AZ_CLIENT_ID"],
            client_secret=os.environ["AZ_CLIENT_SECRET"],
        )
    except KeyError:
        print(
            "エラー: 必要な環境変数（AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET）が設定されていません。"
        )
        return None

    # Cost Management クライアントの初期化
    cost_management_client = CostManagementClient(credential)

    # クエリのスコープ（対象範囲）を設定
    scope = f"/subscriptions/{subscription_id}"

    # Cost Management APIに投げるクエリを定義
    query_definition = QueryDefinition(
        type="ActualCost",  # 実績コストを取得
        timeframe="MonthToDate",  # 期間を「当月初日から本日まで」に設定
        dataset=QueryDataset(
            granularity="Daily",  # 日単位で集計
            aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
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
    for project_name in _project_data.keys():
        cost_results[project_name] = {}

    for cost_data in cost_datas:
        cost_jpy = cost_data[0]
        sub_id = cost_data[2]
        service = cost_data[4]

        project_flag = False
        for project_name, cost_result in cost_results.items():
            if sub_id in _project_data[project_name]["SubscriptionID"]:
                if service in cost_result.keys():
                    cost_result[service] += cost_jpy
                else:
                    cost_result[service] = cost_jpy
                project_flag = True

        if project_flag is False:
            if service in cost_results[DEFAULT_PROJECT].keys():
                cost_results[DEFAULT_PROJECT][service] += cost_jpy
            else:
                cost_results[DEFAULT_PROJECT][service] = cost_jpy

    return cost_results


def create_email_html(sort_cost_data, budget_yen):
    # AWS
    aws_total_cost = sum(sort_cost_data["AWS"].values())
    azure_total_cost = sum(sort_cost_data["Azure"].values())

    all_cost_data = sort_cost_data["AWS"]
    all_cost_data.update(sort_cost_data["Azure"])

    sorted_data = sorted(all_cost_data.items(), key=lambda item: item[1], reverse=True)
    top_10 = sorted_data[:10]
    tbody = ""
    i = 1
    for item, value in top_10:
        tbody += f"""<tr>
                <td>{i}</td>
                <td>{item}</td>
                <td>{value:,.2f} 円</td>
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
                        <td>{aws_total_cost:,.0f} 円</td>
                    </tr>
                    <tr>
                        <td>Azure</td>
                        <td>{azure_total_cost:,.0f} 円</td>
                    </tr>
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
    project_data = get_ssm_parameter()

    # AWS
    aws_cost_datas = get_aws_cost_and_usage()["ResultsByTime"]
    sort_aws_results = sort_out_aws_cost(aws_cost_datas, project_data)

    # Azure
    target_subscription_id = os.environ["AZ_SUB_ID"]
    azure_cost_data = get_azure_cost_and_usage(target_subscription_id)
    sort_azure_results = sort_out_azure_cost(azure_cost_data, project_data)

    # join
    sort_results = {}
    for project, sort_result in sort_aws_results.items():
        sort_results[project] = {}
        sort_results[project]["AWS"] = sort_result
    for project, sort_result in sort_azure_results.items():
        if sort_results.get(project, None) is not None:
            sort_results[project]["Azure"] = sort_result
        else:
            sort_results[project]["Azure"] = sort_result

    for project, sort_result in sort_results.items():
        cost_report = create_email_html(
            sort_result, project_data["project_data"][project]["budget_yen"]
        )
        send_email(project, cost_report)


if __name__ == "__main__":
    lambda_handler({}, {})
