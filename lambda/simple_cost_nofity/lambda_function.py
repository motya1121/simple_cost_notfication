import json
import logging
import os
from datetime import datetime as dt
from datetime import timezone

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
RATE = os.environ.get("RATE_VALUE")


def get_ssm_parameter():
    response = ssm_client.get_parameter(Name=PROJECT_DATA_PARAMETER_NAME)
    parameter_value = json.loads(response["Parameter"]["Value"])
    return parameter_value


def get_cost_and_usage():
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
        Metrics=[
            "UnblendedCost",
        ],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
        ],
    )
    return response


def sort_out_cost(cost_datas, project_data):
    DEFAULT_PROJECT = project_data["default_project"]
    _project_data = project_data["project_data"]
    cost_results = {}
    for project_name in _project_data.keys():
        cost_results[project_name] = {}

    for cost_data in cost_datas:
        for group in cost_data["Groups"]:
            service = group["Keys"][0]
            account_id = group["Keys"][1]
            usd = float(group["Metrics"]["UnblendedCost"]["Amount"])

            project_flag = False
            for project_name, cost_result in cost_results.items():
                if account_id in _project_data[project_name]["AccountID"]:
                    if service in cost_result.keys():
                        cost_result[service] += usd
                    else:
                        cost_result[service] = usd
                    project_flag = True

            if project_flag is False:
                if service in cost_results[DEFAULT_PROJECT].keys():
                    cost_results[DEFAULT_PROJECT][service] += usd
                else:
                    cost_results[DEFAULT_PROJECT][service] = usd

    return cost_results


def create_email_html(sort_cost_data, budget_yen):
    sorted_data = sorted(sort_cost_data.items(), key=lambda item: item[1], reverse=True)
    top_10 = sorted_data[:10]
    tbody = ""
    i = 1
    for item, value in top_10:
        tbody += f"""<tr>
                <td>{i}</td>
                <td>{item}</td>
                <td>{value*RATE:,.2f} 円</td>
            </tr>"""
        i = i + 1

    # 数値の合計値を取得
    total_value = sum(sort_cost_data.values())

    # 今月の予測
    today = dt.now(timezone.utc)
    cost_per_day = total_value / today.day
    predict_month_cost = cost_per_day * 31

    # 予算との差分
    diff_budget_predict = budget_yen - predict_month_cost * RATE

    cost_report = f"""<div>
            <h2>これまでの利用料金</h2>
            <p>{total_value*RATE:,.2f} 円</p>
            <h2>今月の料金予測(31日で計算)</h2>
            <p>{predict_month_cost*RATE:,.2f} 円</p>
            <h2>予算との差分</h2>
            <p>予算({budget_yen:,})-予測({predict_month_cost*RATE:,})= {diff_budget_predict:,} 円</p>
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

    body_html = f"""<html>
    <body>
        <h1>{SUBJECT} - {project}</h1>
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
    cost_datas = get_cost_and_usage()["ResultsByTime"]
    sort_results = sort_out_cost(cost_datas, project_data)
    for project, sort_result in sort_results.items():
        cost_report = create_email_html(
            sort_result, project_data["project_data"][project]["budget_yen"]
        )
        send_email(project, cost_report)


if __name__ == "__main__":
    lambda_handler({}, {})
