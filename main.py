import os
from criteria import RUBRICS
from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel
from typing import List, Dict, Any
import re
import json
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
app = FastAPI(title="AI阅卷服务")

client = OpenAI(
    api_key="sk-af81990cf09d495595bbc6df873dae62",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)


class GradeRequest(BaseModel):
    question_id: str
    image_base64: str


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("，", " ").replace("。", " ").replace("；", " ")
    text = text.replace(",", " ").replace(";", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_recognized_text(text: str) -> str:
    """
    对识别结果做轻量清洗。
    注意：这里只做保守清洗，不要过度删词。
    """
    if not text:
        return ""

    # 去掉常见题号/分值，如 (2)(4分) / （2）（4分）
    text = re.sub(r'[\(（]\d+[\)）]\s*[\(（]\d+\s*分[\)）]', ' ', text)

    # 去掉单独题号，如 (2)
    text = re.sub(r'[\(（]\d+[\)）]', ' ', text)

    # 去掉项目符号序号
    text = re.sub(r'[①②③④⑤⑥⑦⑧⑨⑩]', ' ', text)

    text = normalize_text(text)
    return text


def build_scoring_prompt(question_id: str, recognized_text: str) -> str:
    """
    构建评分 prompt。
    重点：
    1. 允许模型识别未列出的相似表述
    2. 但不能脑补给分
    3. 必须输出 JSON
    """
    if question_id not in RUBRICS:
        raise ValueError(f"未配置题号 {question_id} 的评分规则")

    rubric = RUBRICS[question_id]
    max_points_counted = rubric.get("max_points_counted", rubric["max_score"])

    lines = [
        "请按照 JSON 格式输出结果。",
        f"题号：{question_id}",
        f"满分：{rubric['max_score']}分",
        f"本题最多可计分得分点数：{max_points_counted}"
        "",
        "学生答案：",
        recognized_text,
        "",
        "评分规则：",
        "1. 只能依据学生答案中明确表达的内容评分，不得脑补。",
        "2. 可以识别与得分点语义等价或高度近似的表述，即使它没有出现在“相似表述示例”中。",
        "3. 同一得分点即使出现多个近义表达，也只能计分一次。",
        "4. 学生答案如果表述模糊、泛泛而谈、不能明确对应得分点，则不给分。",
        "5. 只能按给定得分点范围评分，不得创造新的得分点。",
        "6. 本题候选得分点数量可能多于满分对应数量，学生答出其中任意若干个有效得分点即可得分。",
        "7. 若候选得分点总数多于满分允许计分的点数，只选择其中明确命中的点计分，累计到满分即停止，不再重复加分。",
        "8. 输出必须是严格 JSON，不要输出 markdown，不要输出 ```json。",
        "",
        "得分点："
    ]

    for idx, point in enumerate(rubric["points"], start=1):
        lines.append(f"{idx}. 得分点名称：{point['name']}")
        lines.append(f"   分值：{point['score']}")
        if point.get("aliases"):
            lines.append(f"   相似表述示例：{'；'.join(point['aliases'])}")
        lines.append("")

    lines.append(
        """
JSON 输出格式如下：
{
  "recognized_text": "学生答案原文或规范化后的答案",
  "scoring": [
    {
      "point_name": "得分点名称",
      "matched": true,
      "evidence": "学生答案中对应的证据文本",
      "score": 1,
      "reason": "为什么命中或不命中"
    }
  ],
  "total_score": 0,
  "summary_reason": "总评理由"
}
""".strip()
    )

    return "\n".join(lines)


def safe_json_loads(text: str) -> Dict[str, Any]:
    """
    尽量从模型输出中提取 JSON。
    正常情况下 response_format=json_object 已经足够，但这里再做一次兜底。
    """
    text = text.strip()

    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 兜底：从首个 { 到最后一个 } 截取
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError("模型返回的内容不是合法 JSON")


def normalize_model_score(result: Dict[str, Any], question_id: str) -> Dict[str, Any]:
    rubric = RUBRICS[question_id]
    allowed = {p["name"]: p["score"] for p in rubric["points"]}
    max_score = rubric["max_score"]
    max_points_counted = rubric.get("max_points_counted", max_score)

    normalized_scoring = []
    matched_points = []
    seen = set()
    total = 0
    counted_points = 0

    for item in result.get("scoring", []):
        point_name = str(item.get("point_name", "")).strip()
        matched = bool(item.get("matched", False))
        evidence = str(item.get("evidence", "")).strip()
        reason = str(item.get("reason", "")).strip()

        if point_name not in allowed:
            continue

        score = 0
        final_matched = False

        if (
            matched
            and point_name not in seen
            and counted_points < max_points_counted
            and total < max_score
        ):
            score = allowed[point_name]
            if total + score > max_score:
                score = max_score - total

            if score > 0:
                final_matched = True
                seen.add(point_name)
                matched_points.append(point_name)
                total += score
                counted_points += 1

        normalized_scoring.append({
            "point_name": point_name,
            "matched": final_matched,
            "evidence": evidence,
            "score": score,
            "reason": reason
        })

    existing_names = {item["point_name"] for item in normalized_scoring}
    for point in rubric["points"]:
        if point["name"] not in existing_names:
            normalized_scoring.append({
                "point_name": point["name"],
                "matched": False,
                "evidence": "",
                "score": 0,
                "reason": "模型未判定命中"
            })

    return {
        "recognized_text": str(result.get("recognized_text", "")).strip(),
        "scoring": normalized_scoring,
        "matched_points": matched_points,
        "total_score": total,
        "summary_reason": str(result.get("summary_reason", "")).strip() or f"命中 {len(matched_points)} 个得分点，共 {total} 分"
    }


def score_with_model(question_id: str, recognized_text: str) -> Dict[str, Any]:
    """
    第二次调用：纯文本评分
    """
    if question_id not in RUBRICS:
        raise ValueError(f"未配置题号 {question_id} 的评分规则")

    recognized_text = clean_recognized_text(recognized_text)
    if not recognized_text:
        return {
            "recognized_text": "",
            "scoring": [],
            "matched_points": [],
            "total_score": 0,
            "summary_reason": "未识别到有效答案"
        }

    prompt = build_scoring_prompt(question_id, recognized_text)

    try:
        resp = client.chat.completions.create(
            model="qwen3.5-plus",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是中学地理主观题阅卷助手。"
                        "你必须严格按照 JSON 输出。"
                        "不得输出 JSON 之外的任何内容。"
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            response_format={"type": "json_object"},
            temperature=0,
            # 若你后面切到思考模型，结构化输出时要关掉 thinking
            # enable_thinking=False,
        )

        raw_content = resp.choices[0].message.content
        parsed = safe_json_loads(raw_content)
        normalized = normalize_model_score(parsed, question_id)

        # 如果模型没有回填 recognized_text，就用本地识别文本补上
        if not normalized["recognized_text"]:
            normalized["recognized_text"] = recognized_text

        return normalized

    except Exception as e:
        raise RuntimeError(f"评分模型调用失败: {e}")


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
        # 第一次调用：识别
        recognized_text = recognize_answer_with_model(
            image_base64=req.image_base64,
            question_id=req.question_id
        )
        recognized_text = clean_recognized_text(recognized_text)
        logger.info(f"recognized_text: {recognized_text}")
        # 第二次调用：评分
        scored = score_with_model(
            question_id=req.question_id,
            recognized_text=recognized_text
        )

        return {
            "success": True,
            "question_id": req.question_id,
            "recognized_text": scored["recognized_text"],
            "matched_points": scored["matched_points"],
            "score": scored["total_score"],
            "reason": scored["summary_reason"],
            "scoring_detail": scored["scoring"]
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e)
        }