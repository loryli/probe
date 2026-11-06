"""PDF 文字提取与本地 OCR；不上传文档，不执行材料中的指令。"""

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from interview_assistant.materials.errors import MaterialError
from interview_assistant.materials.schemas import MAX_TEXT_BYTES
from interview_assistant.materials.text_layout import format_resume_text, layout_ocr_text

OcrMode = Literal["auto", "force", "off"]
EXTRACTION_VERSION = "pdf-v3-layout-rapidocr3.10-ppocrv6-180dpi"
# PDFium 不允许不同线程并行调用；锁也保护复用的 OCR 引擎。
_ocr_lock = Lock()
logger = logging.getLogger(__name__)


@dataclass
class PdfText:
    text: str
    page_count: int
    warnings: list[str]
    ocr_pages: list[int] = field(default_factory=list)
    page_methods: list[str] = field(default_factory=list)
    cacheable: bool = True


@lru_cache(maxsize=1)
def _engine(model_dir: str) -> Any:
    from rapidocr import EngineType, RapidOCR

    Path(model_dir).mkdir(parents=True, exist_ok=True)
    return RapidOCR(params={
        "Global.model_root_dir": model_dir,
        "Global.log_level": "error",
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Cls.engine_type": EngineType.ONNXRUNTIME,
        "Rec.engine_type": EngineType.ONNXRUNTIME,
    })


def _ocr_error(error: Exception, stage: str) -> MaterialError:
    # 仅记录固定阶段与异常类型，不记录可能含有文档、路径或连接信息的异常正文。
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(chain) < 8 and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    names = {type(item).__name__ for item in chain}
    logger.warning(
        "OCR failed; stage=%s error_type=%s cause_types=%s",
        stage, type(error).__name__, ",".join(sorted(names)),
    )
    if "DownloadFileException" in names:
        return MaterialError(
            "ocr_model_download_failed",
            "OCR 模型下载失败：请确认模型下载网络可用后重试；pip 镜像仅加速依赖安装，不影响模型下载",
            503,
        )
    if any(isinstance(item, PermissionError) for item in chain):
        return MaterialError(
            "ocr_permission_denied",
            "OCR 文件访问被拒绝，请检查 PDF 解析依赖和模型目录权限，或将 INTERVIEW_OCR_MODEL_DIR 改为可写目录",
            503,
        )
    if any(isinstance(item, ImportError) for item in chain):
        return MaterialError(
            "ocr_dependency_missing",
            "OCR 依赖加载失败，请确认 rapidocr、onnxruntime、pypdfium2 已安装；若已安装，请查看终端异常类型",
            503,
        )
    messages = {
        "initialization": "OCR 引擎初始化失败，请查看终端中的错误阶段与异常类型",
        "pdf_render": "PDF 页面渲染失败，请尝试重新导出 PDF 或手工补充文字",
        "recognition": "OCR 模型加载或推理失败，请查看终端中的异常类型后定位模型或运行库问题",
        "result": "OCR 结果处理失败，请查看终端中的异常类型",
    }
    return MaterialError(
        "ocr_unavailable", messages.get(stage, "OCR 运行库加载失败，请查看终端中的异常类型"), 503
    )


def _recognize_page(content: bytes, index: int, model_dir: Path) -> tuple[str, bool]:
    stage = "dependency_import"
    try:
        # 在锁内导入与关闭 PDFium 对象，避免后台线程中的并发 native 调用。
        with _ocr_lock:
            import pypdfium2 as pdfium

            stage = "initialization"
            engine = _engine(str(model_dir.resolve()))
            stage = "pdf_render"
            document = pdfium.PdfDocument(content)
            try:
                page = document[index]
                try:
                    width, height = page.get_size()
                    if min(width, height) <= 0:
                        raise ValueError("Invalid page dimensions")
                    scale = min(180 / 72, 2600 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        # 默认 PDFium 位图为 BGR，与 RapidOCR 的 numpy 输入一致。
                        stage = "recognition"
                        result = engine(bitmap.to_numpy())
                        stage = "result"
                        texts = getattr(result, "txts", None)
                        scores = getattr(result, "scores", None)
                        text = layout_ocr_text(
                            [str(item) for item in texts], getattr(result, "boxes", None)
                        ) if texts is not None else ""
                        uncertain = scores is not None and any(float(score) < 0.75 for score in scores)
                        return text, uncertain
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
    except Exception as error:
        raise _ocr_error(error, stage) from None


def extract_pdf(content: bytes, mode: OcrMode, model_dir: Path) -> PdfText:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise MaterialError("pdf_dependency_missing", "请先安装项目依赖，然后重启服务", 503) from None
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise MaterialError("encrypted_pdf", "暂不支持加密 PDF，请先在本机导出未加密副本")
        count = len(reader.pages)
        if not 1 <= count <= 50:
            raise MaterialError("pdf_page_limit", "简历 PDF 需包含 1–50 页")
        output = PdfText("", count, [
            "已按视觉行与段落整理断行；请核对文字顺序、数字与专有名词。原始 PDF 单独保留。",
            "排版整理不修复识别缺字或杂字符，疑似错误请对照 PDF 原件修正。",
        ])
        pages: list[str] = []
        size = 0
        ocr_failed = False
        for index, page in enumerate(reader.pages):
            method = "text"
            text = ""
            if mode != "force":
                stream = page.get_contents()
                if stream is not None and len(stream.get_data()) > 10 * 1024 * 1024:
                    raise MaterialError("pdf_complexity_limit", "PDF 页面内容过大，请导出精简副本")
                try:
                    text = format_resume_text(page.extract_text() or "")
                except Exception:
                    output.warnings.append(f"第 {index + 1} 页直接提取失败。")
            # 文字很少时可能只有扫描页页眉；整页 OCR 替换该页文字，不与页眉重复拼接。
            needs_ocr = mode == "force" or (
                mode == "auto" and sum(character.isalnum() for character in text) < 20
            )
            if needs_ocr and not ocr_failed:
                try:
                    recognized, uncertain = _recognize_page(content, index, model_dir)
                    if recognized.strip():
                        text, method = recognized, "ocr"
                        output.ocr_pages.append(index + 1)
                        if uncertain:
                            output.warnings.append(f"第 {index + 1} 页存在置信度较低的识别文字，请重点核对。")
                    else:
                        output.warnings.append(f"第 {index + 1} 页 OCR 未识别到文字，请手工补充。")
                except MaterialError as error:
                    if mode == "force":
                        raise
                    output.warnings.append(error.message)
                    ocr_failed = True
                    output.cacheable = False
            if not text.strip():
                method = "empty"
                output.warnings.append(f"第 {index + 1} 页没有可用文字，请核对原件并手工补充。")
            size += len(text.encode("utf-8")) + 2
            if size > MAX_TEXT_BYTES:
                raise MaterialError("pdf_text_limit", "提取文字超过 1 MiB，请精简简历")
            pages.append(text)
            output.page_methods.append(method)
        output.text = "\n\n".join(pages)
        if output.ocr_pages:
            output.warnings.append("OCR 页码：" + "、".join(map(str, output.ocr_pages)) + "。识别文字可在保存前修正。")
        return output
    except MaterialError:
        raise
    except Exception:
        raise MaterialError("invalid_pdf", "PDF 解析失败，请重新导出或手工粘贴文字") from None
