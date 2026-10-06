"""Have the model write Python that uses the market tool inside Docker."""

import argparse
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from corpcom.sandbox import run_python


EXECUTE_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_python",
        "description": (
            "Run Python in Docker. Import corpcom.market.get_price_history. "
            "Its signature is get_price_history(symbol, start_date, end_date); "
            "it returns a dict with rows=[{'date': ISO date, 'close': number}], "
            "source, adjustment, currency, and symbol. Write a compact result.json."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Complete Python program to execute"}
            },
            "required": ["code"],
        },
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two A-share stocks using PTC in Docker")
    parser.add_argument("symbol_a")
    parser.add_argument("symbol_b")
    parser.add_argument("start_date")
    parser.add_argument("end_date")
    args = parser.parse_args()

    load_dotenv()
    client = OpenAI()
    messages = [
        {
            "role": "system",
            "content": (
                "你是 A 股研究助手。必须调用 execute_python，让你生成的 Python 在 Docker 中运行。"
                "代码中使用 from corpcom.market import get_price_history，分别取得两只股票的真实行情。"
                "函数签名为 get_price_history(symbol, start_date, end_date)，返回字典；"
                "data['rows'] 是按日期排序的列表，每项包含 date 和 close；"
                "data['source'] 和 data['adjustment'] 给出来源及复权方式。"
                "在容器内计算首末交易日、前复权收盘价与区间涨跌幅。"
                "将简短 JSON 结果写入当前目录的 result.json；只写摘要，不写原始日线。"
                "JSON 包含每只股票的代码、实际首末日期与价格、涨跌幅、来源、复权方式。"
                "不要打印原始行情或检查工具源码。"
                "工具返回执行错误时可修正代码再试一次。最终回答须依据工具结果，展示计算式。"
                "工具没有返回公司名称或行业，不要补充这些信息。"
                "result.json 保存在沙箱运行目录，目录路径由命令行另行显示。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"比较 {args.symbol_a} 和 {args.symbol_b} 在 "
                f"{args.start_date} 至 {args.end_date} 的股价表现。"
            ),
        },
    ]
    succeeded = False
    attempts = 0

    for _ in range(4):
        response = client.chat.completions.create(
            model=os.environ["CORPCOM_MODEL"],
            messages=messages,
            tools=[EXECUTE_TOOL],
            temperature=0,
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            if not succeeded:
                raise RuntimeError("model answered without successful sandbox execution")
            print(message.content)
            return

        for call in message.tool_calls:
            if call.function.name != "execute_python":
                raise ValueError(f"unknown tool: {call.function.name}")
            attempts += 1
            if attempts > 2:
                raise RuntimeError("model exceeded two sandbox attempts")
            code = json.loads(call.function.arguments)["code"]
            result = run_python(code)
            succeeded = result["status"] == "ok"
            print(f"sandbox run: {result['run_dir']} ({result['status']})")
            if not succeeded:
                print(result["error"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        {key: value for key, value in result.items() if key != "run_dir"},
                        ensure_ascii=False,
                    ),
                }
            )

    raise RuntimeError("model did not finish after four rounds")
