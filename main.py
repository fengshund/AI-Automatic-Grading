import os

from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel
from typing import List, Dict, Any
import re
import json

app = FastAPI(title="AI阅卷服务")

client = OpenAI(
    api_key="sk-af81990cf09d495595bbc6df873dae62",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)


class GradeRequest(BaseModel):
    question_id: str
    image_base64: str


# 你只批一个大题时，就把这个大题所有小问都配进去
# 例如 26-2、26-3
RUBRICS: Dict[str, Dict[str, Any]] = {
    "26-2": {
        "max_score": 4,
        "points": [
            {"aliases": ["环境污染", "污染严重"], "score": 1},
            {"aliases": ["就业困难", "找工作难", "失业增加"], "score": 1},
            {"aliases": ["交通拥挤", "交通堵塞", "交通拥堵", "拥堵"], "score": 1},
            {"aliases": ["住房紧张", "住房困难"], "score": 1}
        ]
    },
    "26-3": {
        "max_score": 4,
        "points": [
            {"aliases": ["城镇加快发展", "城市发展加快", "城市化加快"], "score": 2},
            {"aliases": ["吸引农村人口", "吸引乡村人口", "人口向城镇集中"], "score": 1},
            {"aliases": ["增加绿化面积", "绿化面积增加", "绿地增加"], "score": 1}
        ]
    }
}


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("，", " ").replace("。", " ").replace("；", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def score_text(question_id: str, text: str):
    if question_id not in RUBRICS:
        raise ValueError(f"未配置题号 {question_id} 的评分规则")

    rubric = RUBRICS[question_id]
    total = 0
    matched_points: List[str] = []

    for point in rubric["points"]:
        aliases = point["aliases"]
        point_score = point["score"]
        if any(alias in text for alias in aliases):
            total += point_score
            matched_points.append(aliases[0])

    total = min(total, rubric["max_score"])
    reason = f"命中 {len(matched_points)} 个得分点，共 {total} 分"
    return total, matched_points, reason


def recognize_answer_with_model(image_base64: str, question_id: str) -> str:
    try:
        if not image_base64.startswith("data:"):
            image_base64 = f"data:image/png;base64,{image_base64}"

        prompt = f"""
        你是考试手写答案识别助手。

        请只转写学生手写作答内容，忽略以下内容：
        1. 题号，如“(2)”“(3)”
        2. 分值，如“(4分)”
        3. 非当前小问的作答内容
        4. 题干、序号、括号编号，如“①②③”
        5. 页面上的其他印刷文字

        要求：
        1. 只输出当前小问的学生答案文字
        2. 不要输出题号、分值、括号、序号
        3. 不要解释，不要打分
        4. 多个要点之间用空格分隔
        5. 如果图片中出现多个小问，只识别最上方这一个小问的答案
        当前小题题号：{question_id}
        """.strip()

        stream = client.chat.completions.create(
            model="qwen3.5-flash",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_base64}
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ],
            stream=True,
            stream_options={"include_usage": True},
            temperature=0
        )

        parts = []
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                parts.append(delta.content)

        text = "".join(parts).strip()
        if not text:
            raise RuntimeError("模型返回为空")
        return text

    except Exception as e:
        raise RuntimeError(f"模型识别失败: {e}")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/grade")
def grade(req: GradeRequest):
    try:
        recognized_text = recognize_answer_with_model(
            image_base64=req.image_base64,
            question_id=req.question_id
        )
        recognized_text = normalize_text(recognized_text)

        score, matched_points, reason = score_text(req.question_id, recognized_text)

        return {
            "success": True,
            "question_id": req.question_id,
            "recognized_text": recognized_text,
            "matched_points": matched_points,
            "score": score,
            "reason": reason
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }