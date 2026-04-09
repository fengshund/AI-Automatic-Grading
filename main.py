import os

from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel
from typing import List, Dict, Any
import re
import json

app = FastAPI(title="AI阅卷服务")

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
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
    """
    用 Qwen Omni 识别学生手写答案，只返回转写文本。
    image_base64: 形如 data:image/png;base64,xxxx 或纯 base64 字符串
    """
    model_name = "qwen3.5-omni-plus-2026-03-15"

    # 兼容 data URL / 纯 base64
    if image_base64.startswith("data:"):
        image_url = image_base64
    else:
        image_url = f"data:image/png;base64,{image_base64}"

    prompt = f"""
你是考试手写答案识别助手。
当前题号：{question_id}

请只做一件事：准确转写图片中的学生手写中文答案。
要求：
1. 只输出识别后的答案文本
2. 不要解释
3. 不要打分
4. 不要补充图片中没有的内容
5. 多个要点之间用空格分隔
6. 如果有不确定的字，尽量按上下文识别
"""

    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "你是一个擅长识别中文手写考试答案的助手。"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt.strip()},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    }
                ]
            }
        ],
        temperature=0
    )

    text = resp.choices[0].message.content.strip()
    return text


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