import csv
import base64
import html
import io
import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal

import cv2
import jieba.posseg as pseg
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from rapidocr_onnxruntime import RapidOCR

try:
    from zai import ZhipuAiClient
except Exception:
    ZhipuAiClient = None  # type: ignore[assignment]


DATA_FILE = Path(__file__).resolve().parent.parent / "data.json"
SAMPLE_OCR_FILE = Path(__file__).resolve().parent.parent.parent / "test.png"
LLM_ENV_FILE = Path(__file__).resolve().parent.parent / "LLM.env"


class OntologyNode(BaseModel):
    id: str
    name: str = Field(min_length=1)
    x: float
    y: float
    parent_id: str | None = None
    attributes: list[dict[str, str]] = Field(default_factory=list)


class OntologyEdge(BaseModel):
    id: str
    source: str
    target: str
    relation: str = Field(min_length=1)
    kind: Literal["parent-child", "relation"]
    characteristics: list[str] = Field(default_factory=list)  # ["symmetric", "transitive", "functional"]


class InferenceRule(BaseModel):
    id: str
    rel1: str
    rel2: str
    inferred_rel: str


class MutexRule(BaseModel):
    id: str
    rel1: str
    rel2: str


class GraphState(BaseModel):
    nodes: list[OntologyNode] = Field(default_factory=list)
    edges: list[OntologyEdge] = Field(default_factory=list)
    inference_rules: list[InferenceRule] = Field(default_factory=list)
    mutex_rules: list[MutexRule] = Field(default_factory=list)


class CreateNodePayload(BaseModel):
    name: str = Field(min_length=1)
    x: float
    y: float
    parent_id: str | None = None
    attributes: list[dict[str, str]] = Field(default_factory=list)


class CreateEdgePayload(BaseModel):
    source: str
    target: str
    relation: str = Field(min_length=1)
    kind: Literal["parent-child", "relation"] = "relation"
    characteristics: list[str] = Field(default_factory=list)


class ReparentNodePayload(BaseModel):
    new_parent_id: str | None = None


class OcrExtractResponse(BaseModel):
    mode: Literal["table", "sentence", "mixed"]
    raw_text: str = ""
    entities: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)


class OcrImportPayload(BaseModel):
    entities: list[str] = Field(default_factory=list)
    start_x: float = 120
    start_y: float = 120
    spacing_x: float = 180
    spacing_y: float = 120
    parent_id: str | None = None


class OcrImportResult(BaseModel):
    created_count: int
    skipped_entities: list[str] = Field(default_factory=list)
    nodes: list[OntologyNode] = Field(default_factory=list)


app = FastAPI(title="Ontology Canvas API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


llm_env = _load_env_file(LLM_ENV_FILE)


def _env_int(name: str, default: int) -> int:
    try:
        return int(llm_env.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(llm_env.get(name, str(default)))
    except Exception:
        return default


# 统一 LLM 配置（全部来自 backend/LLM.env，保留旧键兼容）
LLM_PROVIDER = llm_env.get("LLM_PROVIDER", "siliconflow")
LLM_API_URL = llm_env.get("LLM_API_URL", "https://api.siliconflow.cn/v1/chat/completions")
LLM_API_KEY = llm_env.get("LLM_API_KEY", llm_env.get("API_KEY", ""))
LLM_MODEL = llm_env.get("LLM_MODEL", "deepseek-ai/DeepSeek-OCR")
LLM_TIMEOUT_SECONDS = _env_int("LLM_TIMEOUT_SECONDS", 90)
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 4096)
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.0)
LLM_OCR_PRIMARY_PROMPT = llm_env.get("LLM_OCR_PRIMARY_PROMPT", "Free OCR. Please accurately recognize physical quantity symbols (e.g., Am, pH, Q_p, A_pipe, etc.) and DO NOT use LaTeX format (like $Q_{p}$) for symbols. Output them directly as plain text like Q_p or Am. Avoid replacing letters with underscores (e.g., do not output A_ for Am). Do not output irrelevant metadata like model names.")
LLM_OCR_FALLBACK_PROMPT = llm_env.get(
    "LLM_OCR_FALLBACK_PROMPT",
    "<|grounding|>Convert the document to markdown. Please accurately recognize English symbols as plain text without LaTeX wrapping.",
)


def load_graph() -> GraphState:
    if not DATA_FILE.exists():
        return GraphState()
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return GraphState.model_validate(raw)
    except Exception:
        return GraphState()


def save_graph(graph: GraphState) -> None:
    DATA_FILE.write_text(
        json.dumps(graph.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


graph_state = load_graph()
ocr_engine = RapidOCR()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_text(text: str) -> str:
    # 去掉常见不可见字符，统一换行，减少前端出现“乱码感”
    normalized = (
        text.replace("\ufeff", "")
        .replace("\u200b", "")
        .replace("\u3000", " ")
        .replace("\x00", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    return normalized.strip()


def _decode_uploaded_text(content: bytes) -> str:
    # 先按 BOM / 明确特征判断，避免 UTF-8 被误判为 GB 乱码
    if content.startswith(b"\xef\xbb\xbf"):
        return _normalize_text(content.decode("utf-8-sig", errors="strict"))
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return _normalize_text(content.decode("utf-16", errors="strict"))

    # UTF-8 成功则直接使用（最常见且最不该被二次误判）
    try:
        return _normalize_text(content.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        pass

    # 若出现大量空字节，优先按 UTF-16 变体尝试
    zero_ratio = content.count(0) / max(1, len(content))
    if zero_ratio > 0.15:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return _normalize_text(content.decode(enc, errors="strict"))
            except UnicodeDecodeError:
                continue

    # 兼容常见中文编码
    for enc in ("gb18030", "gbk", "big5"):
        try:
            return _normalize_text(content.decode(enc, errors="strict"))
        except UnicodeDecodeError:
            continue

    # 最后兜底：避免抛错，尽量返回可读内容
    return _normalize_text(content.decode("utf-8", errors="replace"))


def _normalize_symbol(text: str) -> str:
    """针对物理量符号进行归一化，例如处理 OCR 可能产生的空格"""
    # 去除两端可能多余的下划线
    text = text.strip("_")
    
    # 修复常见 OCR 识别错误，如 Am 被识别为 A_
    if text == "A_":
        return "Am"
    # 如果是类似 P perf, TDS in 这种模式，尝试合并
    if re.match(r"^[A-Za-z]\s+[a-z0-9]{1,4}$", text):
        return text.replace(" ", "")
    return text


def _extract_nouns_from_text(text: str) -> list[str]:
    nouns: list[str] = []
    # 提取可能的物理量符号 (如 Pperf, Q_in, TDS_out, CO2, Am)
    # 模式：字母开头，后面跟着字母数字或下划线，不依赖 \b 避免中文边界匹配失败
    symbols = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{0,15}(?![A-Za-z0-9_])", text)
    nouns.extend(symbols)

    # 结巴分词可能会把 Q_p 切分为 'Q', '_', 'p'，并且都标注为 'eng'
    # 为了避免把符号拆散的部分重复加入实体列表，我们要先构建已提取符号的所有可能碎片
    symbol_parts: set[str] = set(symbols)
    for sym in symbols:
        if "_" in sym:
            symbol_parts.update(sym.split("_"))

    for word, flag in pseg.cut(text):
        w = word.strip()
        if not w:
            continue
        # 排除已经被识别为符号的整体或被拆散的部分
        if w in symbol_parts:
            continue
        if flag.startswith("n") or flag in {"vn", "eng"}:
            nouns.append(w)

    result = _dedupe_keep_order([_normalize_symbol(n) for n in nouns])
    if result:
        return result

    # 若名词提取过少，回退到按逗号/句号切分的短语
    fallback = [seg.strip() for seg in re.split(r"[，。；;、\n]+", text) if seg.strip()]
    return _dedupe_keep_order(fallback)


def _group_rows_by_y(cells: list[dict]) -> list[list[dict]]:
    if not cells:
        return []

    heights = [max(8.0, cell["h"]) for cell in cells]
    median_h = sorted(heights)[len(heights) // 2]
    row_tol = max(16.0, median_h * 0.8)

    rows: list[dict] = []
    for cell in sorted(cells, key=lambda x: x["cy"]):
        placed = False
        for row in rows:
            if abs(cell["cy"] - row["y"]) <= row_tol:
                row["cells"].append(cell)
                row["y"] = (row["y"] * row["count"] + cell["cy"]) / (row["count"] + 1)
                row["count"] += 1
                placed = True
                break
        if not placed:
            rows.append({"y": cell["cy"], "cells": [cell], "count": 1})

    result: list[list[dict]] = []
    for row in rows:
        result.append(sorted(row["cells"], key=lambda x: x["cx"]))
    return result


def _file_suffix_to_mime(suffix: str) -> str:
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
    }
    return mapping.get(suffix, "image/png")


def _clean_llm_ocr_text(text: str) -> str:
    cleaned = text or ""
    # DeepSeek-OCR 在部分模式下会输出定位标记，直接去掉避免污染实体
    tag_patterns = [
        r"<\|ref\|>",
        r"<\|det\|>",
        r"<\|box_start\|>",
        r"<\|box_end\|>",
        r"<boxstart>",
        r"<boxend>",
        r"\bboxstart\b",
        r"\bboxend\b",
    ]
    for pattern in tag_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        
    # 处理可能的 LaTeX 数学模式包裹，如 $Am$ 或 \(Am\)
    cleaned = re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", r"\1", cleaned)
    cleaned = re.sub(r"\\\((.*?)\\\)", r"\1", cleaned)
    
    # 针对带有 LaTeX 下标大括号的情况进行合并，例如 Q_{p} -> Q_p 或 A_{pipe} -> A_pipe
    # 这里匹配类似于 A_{bc} 或 A_{12}
    cleaned = re.sub(r"([A-Za-z]+)_\{([A-Za-z0-9]+)\}", r"\1_\2", cleaned)
    
    # 移除被错误加上下划线的末尾，比如 Q_ -> Q (只处理孤立的或者在词尾的下划线)
    # 但要注意不要破坏真正的连字符（比如 Q_in），所以只处理后面是空格或标点的情况
    cleaned = re.sub(r"\b([A-Za-z]+)_(\s|$|[.,;!?)\]])", r"\1\2", cleaned)
    
    return _normalize_text(cleaned)


def _looks_like_invalid_llm_output(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    # 典型异常：仅返回括号碎片，如 }]}]}]}
    if len(s) <= 20 and re.fullmatch(r"[\[\]\{\}\s,.:]+", s):
        return True
    return False


def _extract_table_rows_from_markdown(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        raw = line.strip()
        if raw.count("|") < 2:
            continue
        if re.fullmatch(r"[\|\-\s:]+", raw):
            continue
        cells = [c.strip() for c in raw.strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    return rows


def _strip_html_tags(text: str) -> str:
    no_script = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    no_style = re.sub(r"<style[\s\S]*?</style>", "", no_script, flags=re.IGNORECASE)
    no_tags = re.sub(r"<[^>]+>", " ", no_style)
    return _normalize_text(html.unescape(re.sub(r"\s+", " ", no_tags)))


def _extract_table_rows_from_html(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", text, flags=re.IGNORECASE):
        cells: list[str] = []
        for cell_html in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, flags=re.IGNORECASE):
            cell_text = _strip_html_tags(cell_html)
            cells.append(cell_text)
        if any(cells):
            rows.append(cells)
    return rows


def _extract_entities_from_table_rows(table_rows: list[list[str]]) -> list[str]:
    # 用户要求：表格每一格内容都要完整且独立地进入候选实体
    cells: list[str] = []
    for row in table_rows:
        for cell in row:
            value = _normalize_text(str(cell))
            if value:
                cells.append(value)
    return _dedupe_keep_order(cells)


def _to_plain_data(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, list):
        return [_to_plain_data(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _to_plain_data(v) for k, v in obj.items()}
    for method in ("model_dump", "dict", "to_dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                return _to_plain_data(fn())
            except Exception:
                continue
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict) and attrs:
        return _to_plain_data(attrs)
    return str(obj)


def _collect_text_fields(data: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(data, str):
        value = data.strip()
        if value:
            texts.append(value)
        return texts
    if isinstance(data, list):
        for item in data:
            texts.extend(_collect_text_fields(item))
        return texts
    if isinstance(data, dict):
        preferred_keys = ("text", "content", "raw_text", "markdown", "result", "output")
        found_preferred = False
        for key in preferred_keys:
            if key in data:
                found_preferred = True
                texts.extend(_collect_text_fields(data.get(key)))
        
        # 如果命中了常见字段，就不再深入遍历，避免提取到 model="glm-4v" 等元数据
        if found_preferred:
            return texts
            
        # 若没有命中，只递归那些看起来像数据的字段，避免元数据
        for key, value in data.items():
            if str(key).lower() not in ("model", "id", "object", "created", "task_id", "status"):
                texts.extend(_collect_text_fields(value))
    return texts


def _call_llm_ocr(image_b64: str, mime: str, prompt: str) -> str:
    if LLM_PROVIDER.lower() in {"zhipu", "zhipuai", "glm", "zai"}:
        if ZhipuAiClient is None:
            raise HTTPException(
                status_code=500,
                detail="未安装 zai-sdk，无法使用智谱 OCR。请在 backend/requirements.txt 添加 zai-sdk 并安装依赖。",
            )
        try:
            client = ZhipuAiClient(api_key=LLM_API_KEY)
            # zai-sdk 支持传入公网 URL 或本地文件。这里用 data URL 以兼容前端上传文件。
            data_url = f"data:{mime};base64,{image_b64}"
            resp = client.layout_parsing.create(model=LLM_MODEL, file=data_url)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"智谱 OCR 调用异常: {exc}") from exc

        # 尽量从响应中提取可用文本：优先 text/content 等字段，其次整体 JSON
        plain = _to_plain_data(resp)
        texts = _collect_text_fields(plain)
        if texts:
            return "\n".join(_dedupe_keep_order(texts))
        if isinstance(plain, (dict, list)):
            return json.dumps(plain, ensure_ascii=False)
        return str(plain)

    # 默认：OpenAI 兼容 chat/completions（例如硅基流动 DeepSeek-OCR）
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        LLM_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"LLM OCR 调用失败: {error_text}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM OCR 调用异常: {exc}") from exc

    choices = response_payload.get("choices", [])
    if not choices:
        raise HTTPException(status_code=502, detail="LLM OCR 返回为空")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    if not isinstance(content, str):
        content = str(content)
    return content


def _parse_ocr_image_with_llm(image_bytes: bytes, suffix: str) -> OcrExtractResponse:
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 LLM API_KEY，请检查 backend/LLM.env")

    mime = _file_suffix_to_mime(suffix)
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    # DeepSeek-OCR 推荐使用固定短提示词而非强 schema 约束
    # 先用 Free OCR；若返回疑似异常碎片，再回退到 grounding 模式
    content = _call_llm_ocr(image_b64, mime, LLM_OCR_PRIMARY_PROMPT)
    cleaned_text = _clean_llm_ocr_text(content)
    if _looks_like_invalid_llm_output(cleaned_text):
        content = _call_llm_ocr(image_b64, mime, LLM_OCR_FALLBACK_PROMPT)
        cleaned_text = _clean_llm_ocr_text(content)
    if _looks_like_invalid_llm_output(cleaned_text):
        # 双次 LLM 返回都不可信时，回退到本地 OCR，保证接口可用性
        return _parse_ocr_image(image_bytes)

    if not cleaned_text:
        raise HTTPException(status_code=502, detail="LLM OCR 返回内容为空")

    table_rows_md = _extract_table_rows_from_markdown(cleaned_text)
    table_rows_html = _extract_table_rows_from_html(cleaned_text)
    table_rows = table_rows_html if len(table_rows_html) >= len(table_rows_md) else table_rows_md
    # 表格场景：严格按每个单元格独立提取；非表格：提取名词性本体概念
    plain_text = _strip_html_tags(cleaned_text)
    entities = _extract_entities_from_table_rows(table_rows) if table_rows else _extract_nouns_from_text(plain_text)
    if table_rows and entities:
        mode: Literal["table", "sentence", "mixed"] = "table"
    elif table_rows:
        mode = "mixed"
    else:
        mode = "sentence"
    return OcrExtractResponse(
        mode=mode,
        raw_text=cleaned_text,
        entities=_dedupe_keep_order(entities),
        table_rows=table_rows,
    )


def _parse_ocr_image(image_bytes: bytes) -> OcrExtractResponse:
    np_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="无法解码图片，请检查文件格式")

    ocr_result, _ = ocr_engine(image)
    if not ocr_result:
        return OcrExtractResponse(mode="sentence", raw_text="", entities=[], table_rows=[])

    cells: list[dict] = []
    for item in ocr_result:
        if len(item) < 2:
            continue
        box = item[0]
        text = _normalize_text(str(item[1]))
        if not text:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        cells.append(
            {
                "text": text,
                "cx": (x_min + x_max) / 2,
                "cy": (y_min + y_max) / 2,
                "w": x_max - x_min,
                "h": y_max - y_min,
            }
        )

    if not cells:
        return OcrExtractResponse(mode="sentence", raw_text="", entities=[], table_rows=[])

    rows = _group_rows_by_y(cells)
    row_lengths = [len(r) for r in rows]
    median_cols = sorted(row_lengths)[len(row_lengths) // 2] if row_lengths else 0
    dense_rows = sum(1 for x in row_lengths if x >= max(2, median_cols - 1))
    is_table = len(rows) >= 3 and median_cols >= 3 and dense_rows / max(1, len(rows)) >= 0.6

    if is_table:
        table_rows = [[cell["text"] for cell in row] for row in rows]
        raw_entities = []
        for row in table_rows:
            for cell in row:
                c = cell.strip()
                # Skip empty or pure numbers
                if not c or c.isdigit():
                    continue
                # Skip pure punctuation that are short (not letters/numbers/chinese)
                if len(c) <= 2 and not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in c):
                    continue
                # For tables, usually we want the cell text itself as an entity.
                # If it's too long (>15 chars), it might be a description, so we extract nouns.
                # But we should avoid splitting English sentences into scattered words.
                if len(c) > 15 and any("\u4e00" <= ch <= "\u9fff" for ch in c):
                    nouns = _extract_nouns_from_text(c)
                    # Filter out scattered English words from jieba
                    raw_entities.extend([n for n in nouns if any("\u4e00" <= ch <= "\u9fff" for ch in n) or len(n) > 2])
                else:
                    # Filter out common table headers/captions like "表5-1"
                    if not re.match(r"^(表|图|table|figure)\s*[\d\.\-]+", c, re.IGNORECASE):
                        raw_entities.append(c)
        entities = _dedupe_keep_order(raw_entities)
        raw_text = "\n".join(["\t".join(row) for row in table_rows])
        return OcrExtractResponse(
            mode="table",
            raw_text=raw_text,
            entities=entities,
            table_rows=table_rows,
        )

    sentence_text = " ".join(cell["text"] for row in rows for cell in row)
    entities = _extract_nouns_from_text(sentence_text)
    return OcrExtractResponse(
        mode="sentence",
        raw_text=sentence_text,
        entities=entities,
        table_rows=[],
    )


@app.get("/api/graph", response_model=GraphState)
def get_graph() -> GraphState:
    return graph_state


@app.put("/api/graph", response_model=GraphState)
def replace_graph(payload: GraphState) -> GraphState:
    global graph_state
    graph_state = payload
    save_graph(graph_state)
    return graph_state


@app.post("/api/nodes", response_model=OntologyNode)
def create_node(payload: CreateNodePayload) -> OntologyNode:
    if payload.parent_id and not any(n.id == payload.parent_id for n in graph_state.nodes):
        raise HTTPException(status_code=400, detail="parent_id 对应节点不存在")

    node = OntologyNode(
        id=str(uuid.uuid4()),
        name=payload.name.strip(),
        x=payload.x,
        y=payload.y,
        parent_id=payload.parent_id,
    )
    graph_state.nodes.append(node)

    if payload.parent_id:
        parent_edge = OntologyEdge(
            id=str(uuid.uuid4()),
            source=payload.parent_id,
            target=node.id,
            relation="父子关系",
            kind="parent-child",
        )
        graph_state.edges.append(parent_edge)

    save_graph(graph_state)
    return node


@app.post("/api/edges", response_model=OntologyEdge)
def create_edge(payload: CreateEdgePayload) -> OntologyEdge:
    if payload.source == payload.target:
        raise HTTPException(status_code=400, detail="不能连接到自身")

    all_ids = {n.id for n in graph_state.nodes}
    if payload.source not in all_ids or payload.target not in all_ids:
        raise HTTPException(status_code=400, detail="source 或 target 节点不存在")

    relation_clean = payload.relation.strip()

    # 1. 互斥规则检查
    # 获取 source 和 target 之间已存在的所有关系（不分方向，只要两个节点之间存在即可）
    existing_edges = [
        e for e in graph_state.edges 
        if (e.source == payload.source and e.target == payload.target) or 
           (e.source == payload.target and e.target == payload.source)
    ]
    existing_rels = {e.relation for e in existing_edges}

    for rule in graph_state.mutex_rules:
        if relation_clean == rule.rel1 and rule.rel2 in existing_rels:
            raise HTTPException(status_code=400, detail=f"互斥约束冲突：无法在已有 '{rule.rel2}' 关系上建立 '{rule.rel1}'")
        if relation_clean == rule.rel2 and rule.rel1 in existing_rels:
            raise HTTPException(status_code=400, detail=f"互斥约束冲突：无法在已有 '{rule.rel1}' 关系上建立 '{rule.rel2}'")

    edge = OntologyEdge(
        id=str(uuid.uuid4()),
        source=payload.source,
        target=payload.target,
        relation=relation_clean,
        kind=payload.kind,
    )
    graph_state.edges.append(edge)
    
    # 2. 顺承/推理规则自动连线 (简单的 BFS 传播)
    edges_to_process = [edge]
    # 使用 signature 避免重复添加和无限循环
    seen_signatures = {(e.source, e.target, e.relation) for e in graph_state.edges}

    while edges_to_process:
        curr = edges_to_process.pop(0)

        for rule in graph_state.inference_rules:
            # 模式 A: curr 作为 rel1 (A -> B)，寻找 B -> C 的 rel2
            if curr.relation == rule.rel1:
                for e in graph_state.edges:
                    if e.source == curr.target and e.relation == rule.rel2:
                        sig = (curr.source, e.target, rule.inferred_rel)
                        if sig not in seen_signatures and curr.source != e.target:
                            inferred_edge = OntologyEdge(
                                id=str(uuid.uuid4()),
                                source=curr.source,
                                target=e.target,
                                relation=rule.inferred_rel,
                                kind="relation"
                            )
                            graph_state.edges.append(inferred_edge)
                            edges_to_process.append(inferred_edge)
                            seen_signatures.add(sig)

            # 模式 B: curr 作为 rel2 (B -> C)，寻找 A -> B 的 rel1
            if curr.relation == rule.rel2:
                for e in graph_state.edges:
                    if e.target == curr.source and e.relation == rule.rel1:
                        sig = (e.source, curr.target, rule.inferred_rel)
                        if sig not in seen_signatures and e.source != curr.target:
                            inferred_edge = OntologyEdge(
                                id=str(uuid.uuid4()),
                                source=e.source,
                                target=curr.target,
                                relation=rule.inferred_rel,
                                kind="relation"
                            )
                            graph_state.edges.append(inferred_edge)
                            edges_to_process.append(inferred_edge)
                            seen_signatures.add(sig)

    save_graph(graph_state)
    return edge


@app.post("/api/nodes/{node_id}/reparent", response_model=OntologyNode)
def reparent_node(node_id: str, payload: ReparentNodePayload) -> OntologyNode:
    node_map = {n.id: n for n in graph_state.nodes}
    node = node_map.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")

    if payload.new_parent_id == node_id:
        raise HTTPException(status_code=400, detail="不能将节点设置为自身的子节点")

    if payload.new_parent_id and payload.new_parent_id not in node_map:
        raise HTTPException(status_code=400, detail="new_parent_id 对应节点不存在")

    # 防止环：新父节点不能是当前节点的后代
    probe_id = payload.new_parent_id
    while probe_id:
        if probe_id == node_id:
            raise HTTPException(status_code=400, detail="不能将节点挂载到其后代节点下")
        probe = node_map.get(probe_id)
        probe_id = probe.parent_id if probe else None

    node.parent_id = payload.new_parent_id

    graph_state.edges = [
        edge
        for edge in graph_state.edges
        if not (edge.kind == "parent-child" and edge.target == node_id)
    ]

    if payload.new_parent_id:
        graph_state.edges.append(
            OntologyEdge(
                id=str(uuid.uuid4()),
                source=payload.new_parent_id,
                target=node_id,
                relation="父子关系",
                kind="parent-child",
            )
        )

    save_graph(graph_state)
    return node


@app.post("/api/ocr/extract", response_model=OcrExtractResponse)
@app.post("/ocr/extract", response_model=OcrExtractResponse)
async def ocr_extract(
    file: UploadFile = File(...),
    engine: Literal["local", "llm"] = Query(default="local"),
) -> OcrExtractResponse:
    filename = (file.filename or "").lower()
    suffix = Path(filename).suffix
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")

    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
        if engine == "llm":
            return _parse_ocr_image_with_llm(content, suffix)
        return _parse_ocr_image(content)

    if suffix in {".txt", ".md"}:
        text = _decode_uploaded_text(content)
        entities = _extract_nouns_from_text(text)
        return OcrExtractResponse(mode="sentence", raw_text=text, entities=entities, table_rows=[])

    if suffix == ".csv":
        csv_text = _decode_uploaded_text(content)
        rows = [row for row in csv.reader(io.StringIO(csv_text))]
        raw_entities = []
        for row in rows:
            for cell in row:
                c = cell.strip()
                if not c or c.isdigit():
                    continue
                if len(c) <= 2 and not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in c):
                    continue
                if len(c) > 10:
                    raw_entities.extend(_extract_nouns_from_text(c))
                else:
                    raw_entities.append(c)
        entities = _dedupe_keep_order(raw_entities)
        return OcrExtractResponse(
            mode="table",
            raw_text=csv_text,
            entities=entities,
            table_rows=rows,
        )

    raise HTTPException(status_code=400, detail="暂不支持该文件类型，当前支持：图片/png/jpg、txt、csv")


@app.post("/api/ocr/import", response_model=OcrImportResult)
@app.post("/ocr/import", response_model=OcrImportResult)
def ocr_import(payload: OcrImportPayload) -> OcrImportResult:
    if payload.parent_id and not any(n.id == payload.parent_id for n in graph_state.nodes):
        raise HTTPException(status_code=400, detail="parent_id 对应节点不存在")

    entities = _dedupe_keep_order(payload.entities)
    if not entities:
        return OcrImportResult(created_count=0, skipped_entities=[], nodes=[])

    existing_names = {n.name for n in graph_state.nodes}
    skipped: list[str] = []
    created_nodes: list[OntologyNode] = []

    for i, entity in enumerate(entities):
        if entity in existing_names:
            skipped.append(entity)
            continue

        col = i % 4
        row = i // 4
        node = OntologyNode(
            id=str(uuid.uuid4()),
            name=entity,
            x=payload.start_x + col * payload.spacing_x,
            y=payload.start_y + row * payload.spacing_y,
            parent_id=payload.parent_id,
        )
        graph_state.nodes.append(node)
        created_nodes.append(node)
        existing_names.add(entity)

        if payload.parent_id:
            graph_state.edges.append(
                OntologyEdge(
                    id=str(uuid.uuid4()),
                    source=payload.parent_id,
                    target=node.id,
                    relation="父子关系",
                    kind="parent-child",
                )
            )

    save_graph(graph_state)
    return OcrImportResult(
        created_count=len(created_nodes),
        skipped_entities=skipped,
        nodes=created_nodes,
    )


@app.get("/api/ocr/sample-file")
def get_ocr_sample_file() -> FileResponse:
    if not SAMPLE_OCR_FILE.exists():
        raise HTTPException(status_code=404, detail="示例文件不存在")
    return FileResponse(
        path=SAMPLE_OCR_FILE,
        media_type="image/png",
        filename=SAMPLE_OCR_FILE.name,
    )


if __name__ == "__main__":
    import uvicorn
    # 支持直接用 python main.py 运行
    uvicorn.run(app, host="0.0.0.0", port=8000)
