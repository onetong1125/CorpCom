"""One research run using ordinary model tool calls."""

import argparse
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from corpcom.market import get_price_history


PRICE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_price_history",
        "description": "Get daily forward-adjusted A-share closing prices for one stock and date range.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Six-digit A-share code"},
                "start_date": {"type": "string", "description": "Inclusive YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "Inclusive YYYY-MM-DD"},
            },
            "required": ["symbol", "start_date", "end_date"],
        },
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two A-share stocks with real price data")
    parser.add_argument("symbol_a", help="first six-digit stock code")
    parser.add_argument("symbol_b", help="second six-digit stock code")
    parser.add_argument("start_date", help="inclusive YYYY-MM-DD")
    parser.add_argument("end_date", help="inclusive YYYY-MM-DD")
    args = parser.parse_args()

    load_dotenv()
    model = os.environ["CORPCOM_MODEL"]
    client = OpenAI()
    messages = [
        {
            "role": "system",
            "content": (
                "你是 A 股研究助手。必须先调用工具获取真实行情，再回答。"
                "比较两只股票在指定区间首末交易日的前复权收盘价和区间涨跌幅。"
                "展示计算公式、实际交易日期、股票代码、数据来源和复权方式。"
                "只根据工具结果下结论；不要编造公司名称或投资建议。"
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
    requested = {args.symbol_a, args.symbol_b}
    fetched = set()

    for _ in range(4):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=[PRICE_TOOL], temperature=0
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        if not message.tool_calls:
            if fetched != requested:
                raise RuntimeError(f"model answered before fetching both stocks: {requested - fetched}")
            print(message.content)
            return

        for call in message.tool_calls:
            if call.function.name != "get_price_history":
                raise ValueError(f"unknown tool: {call.function.name}")
            parameters = json.loads(call.function.arguments)
            if (
                parameters["symbol"] not in requested
                or parameters["start_date"] != args.start_date
                or parameters["end_date"] != args.end_date
            ):
                raise ValueError(f"model requested unexpected prices: {parameters}")
            result = get_price_history(**parameters)
            fetched.add(parameters["symbol"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    raise RuntimeError("model did not finish after four tool-call rounds")
