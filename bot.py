import os
import re
import time
from collections import deque
from typing import Optional, Dict, List, Tuple

from PIL import Image, ImageOps, ImageEnhance, ImageFilter

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

# ====== CONFIG ======
TOKEN = os.getenv("BOT_TOKEN") or "8397296110:AAGtA2awgyB3K90TOMJEx4f46f6zoWah230"

MAX_IMAGES_PER_SESSION = 25            # upload limit
MAX_IMAGE_BYTES = 10 * 1024 * 1024     # 10MB per image (document or photo)
MIN_SECONDS_BETWEEN_UPLOADS = 0.8      # basic rate limit
MAX_UPLOADS_PER_60S = 25              # burst limit

PDF_MARGIN_MM = 12.0

# Quality profiles: (max_long_edge_px, jpeg_quality)
QUALITY_PROFILES = {
    "high": (3000, 85),
    "compressed": (1600, 60),
}

# Enhancement default (scan-like look)
DEFAULT_ENHANCE = True

# ====== STATE ======
user_files: Dict[int, List[str]] = {}      # {uid: [downloaded image paths]}
user_pdfname: Dict[int, str] = {}          # {uid: "name_without_ext"}
user_await_name: set[int] = set()          # waiting for name input
user_mode: Dict[int, str] = {}             # merge | separate
user_quality: Dict[int, str] = {}          # high | compressed
user_enhance: Dict[int, bool] = {}         # True | False

# Live status message per user: (chat_id, message_id)
user_status_msg: Dict[int, Tuple[int, int]] = {}

# Rate-limit timestamps per user
user_upload_times: Dict[int, deque] = {}   # {uid: deque([timestamps])}


# ====== UI ======
def kb(uid: int) -> InlineKeyboardMarkup:
    mode = user_mode.get(uid, "merge")
    quality = user_quality.get(uid, "high")
    enhance = user_enhance.get(uid, DEFAULT_ENHANCE)

    mode_label = "Merge 📄" if mode == "merge" else "Separate 📑"
    quality_label = "High ✅" if quality == "high" else "Compressed 🗜️"
    enhance_label = "Enhance ✨ ON" if enhance else "Enhance ✨ OFF"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Done ✅", callback_data="DONE"),
                InlineKeyboardButton("Clear 🧹", callback_data="CLEAR"),
            ],
            [
                InlineKeyboardButton("Set Name 📝", callback_data="SETNAME"),
                InlineKeyboardButton(f"Mode: {mode_label} 🔁", callback_data="MODE"),
            ],
            [
                InlineKeyboardButton(f"Quality: {quality_label} 🔧", callback_data="QUALITY"),
                InlineKeyboardButton(enhance_label, callback_data="ENHANCE"),
            ],
        ]
    )


def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)  # Windows illegal chars
    name = re.sub(r"\s+", " ", name)
    return name[:60] if name else "photos"


def status_text(uid: int) -> str:
    n = len(user_files.get(uid, []))
    mode = user_mode.get(uid, "merge")
    quality = user_quality.get(uid, "high")
    enhance = user_enhance.get(uid, DEFAULT_ENHANCE)
    name = user_pdfname.get(uid, "photos")

    mode_label = "Merge (1 PDF)" if mode == "merge" else "Separate (many PDFs)"
    quality_label = "High" if quality == "high" else "Compressed"
    enhance_label = "ON" if enhance else "OFF"

    return (
        "📄 *Photo → PDF Bot*\n\n"
        f"📸 Images: *{n}* / {MAX_IMAGES_PER_SESSION}\n"
        f"📝 Name: *{name}.pdf*\n"
        f"🔁 Mode: *{mode_label}*\n"
        f"🔧 Quality: *{quality_label}*\n"
        f"✨ Enhance: *{enhance_label}*\n\n"
        "Send more images, or press *Done ✅*."
    )


async def ensure_status(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Create status message once, then edit later."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if not chat_id:
        return

    if uid in user_status_msg:
        await refresh_status(context, uid)
        return

    # create new status message
    msg = None
    if update.message:
        msg = await update.message.reply_text(
            status_text(uid), parse_mode="Markdown", reply_markup=kb(uid)
        )
    elif update.callback_query and update.callback_query.message:
        msg = await update.callback_query.message.reply_text(
            status_text(uid), parse_mode="Markdown", reply_markup=kb(uid)
        )

    if msg:
        user_status_msg[uid] = (chat_id, msg.message_id)


async def refresh_status(context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Edit the existing status message."""
    if uid not in user_status_msg:
        return
    chat_id, msg_id = user_status_msg[uid]
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=status_text(uid),
            parse_mode="Markdown",
            reply_markup=kb(uid),
        )
    except Exception:
        # message may be deleted or too old to edit; just forget it
        user_status_msg.pop(uid, None)


# ====== RATE LIMIT / CHECKS ======
def check_rate_limit(uid: int) -> Optional[str]:
    now = time.time()
    dq = user_upload_times.setdefault(uid, deque())

    # Remove timestamps older than 60s
    while dq and now - dq[0] > 60:
        dq.popleft()

    # burst limit
    if len(dq) >= MAX_UPLOADS_PER_60S:
        return "⛔ Too many uploads in 60 seconds. Please slow down a bit."

    # minimum spacing
    if dq and (now - dq[-1] < MIN_SECONDS_BETWEEN_UPLOADS):
        return "⏳ You're uploading too fast. Wait a moment and try again."

    dq.append(now)
    return None


def check_limits(uid: int) -> Optional[str]:
    if len(user_files.get(uid, [])) >= MAX_IMAGES_PER_SESSION:
        return f"⛔ Upload limit reached ({MAX_IMAGES_PER_SESSION} images). Press Done ✅ or Clear 🧹."
    return None


def check_filesize(bytes_: Optional[int]) -> Optional[str]:
    if bytes_ is None:
        return None
    if bytes_ > MAX_IMAGE_BYTES:
        mb = bytes_ / (1024 * 1024)
        limit_mb = MAX_IMAGE_BYTES / (1024 * 1024)
        return f"⛔ File too large ({mb:.1f}MB). Limit is {limit_mb:.0f}MB."
    return None


# ====== IMAGE PREP / ENHANCE / PDF ======
def enhance_scan(img: Image.Image) -> Image.Image:
    """
    Simple scan-like enhancement:
    - auto contrast
    - slight denoise
    - sharpen
    - slightly increase contrast
    """
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = ImageEnhance.Sharpness(img).enhance(1.4)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    return img


def prepare_image_for_pdf(src_path: str, out_path: str, uid: int) -> str:
    """
    Convert to RGB, fix EXIF rotation, optionally enhance, resize, save as JPEG.
    Returns output path.
    """
    quality = user_quality.get(uid, "high")
    enhance_on = user_enhance.get(uid, DEFAULT_ENHANCE)
    max_edge, jpeg_q = QUALITY_PROFILES[quality]

    img = Image.open(src_path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    if enhance_on:
        img = enhance_scan(img)

    # Resize to limit
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > max_edge:
        scale = max_edge / long_edge
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    img.save(out_path, format="JPEG", quality=jpeg_q, optimize=True)
    return out_path


def build_a4_pdf_from_prepared(prepared_paths: List[str], out_pdf: str, margin_mm: float):
    page_w, page_h = A4
    margin = margin_mm * mm
    avail_w = page_w - 2 * margin
    avail_h = page_h - 2 * margin

    c = canvas.Canvas(out_pdf, pagesize=A4)

    for path in prepared_paths:
        img = Image.open(path)
        img_w, img_h = img.size

        scale = min(avail_w / img_w, avail_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale

        x = (page_w - draw_w) / 2
        y = (page_h - draw_h) / 2

        c.drawImage(ImageReader(img), x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
        c.showPage()

    c.save()


# ====== COMMANDS / HANDLERS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id

    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")
    user_quality.setdefault(uid, "high")
    user_enhance.setdefault(uid, DEFAULT_ENHANCE)

    await ensure_status(update, context, uid)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    uid = update.effective_user.id

    # delete stored images
    for p in user_files.get(uid, []):
        if os.path.exists(p):
            os.remove(p)

    user_files[uid] = []
    user_pdfname.pop(uid, None)
    user_await_name.discard(uid)

    await ensure_status(update, context, uid)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id

    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")
    user_quality.setdefault(uid, "high")
    user_enhance.setdefault(uid, DEFAULT_ENHANCE)

    # Protection checks
    msg = check_rate_limit(uid) or check_limits(uid) or check_filesize(getattr(update.message.photo[-1], "file_size", None))
    if msg:
        await update.message.reply_text(msg)
        await ensure_status(update, context, uid)
        return

    photos = update.message.photo
    if not photos:
        return

    photo = photos[-1]
    tg_file = await photo.get_file()

    img_path = f"{uid}_{update.message.message_id}_photo.jpg"
    await tg_file.download_to_drive(img_path)
    user_files[uid].append(img_path)

    await ensure_status(update, context, uid)
    await refresh_status(context, uid)


async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id

    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")
    user_quality.setdefault(uid, "high")
    user_enhance.setdefault(uid, DEFAULT_ENHANCE)

    doc = update.message.document
    if not doc:
        return

    if not (doc.mime_type and doc.mime_type.startswith("image/")):
        await update.message.reply_text("Please send an image file (JPG/PNG) as Document.")
        await ensure_status(update, context, uid)
        return

    # Protection checks
    msg = check_rate_limit(uid) or check_limits(uid) or check_filesize(getattr(doc, "file_size", None))
    if msg:
        await update.message.reply_text(msg)
        await ensure_status(update, context, uid)
        return

    tg_file = await doc.get_file()

    ext = ".jpg"
    if doc.file_name and "." in doc.file_name:
        ext_guess = "." + doc.file_name.split(".")[-1].lower()
        if ext_guess in [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"]:
            ext = ext_guess

    img_path = f"{uid}_{update.message.message_id}_doc{ext}"
    await tg_file.download_to_drive(img_path)
    user_files[uid].append(img_path)

    await ensure_status(update, context, uid)
    await refresh_status(context, uid)


async def handle_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    if uid not in user_await_name:
        return

    raw = (update.message.text or "").strip()
    fname = sanitize_filename(raw)
    user_pdfname[uid] = fname
    user_await_name.discard(uid)

    await ensure_status(update, context, uid)
    await refresh_status(context, uid)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    uid = query.from_user.id

    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")
    user_quality.setdefault(uid, "high")
    user_enhance.setdefault(uid, DEFAULT_ENHANCE)

    await query.answer()
    action = query.data

    if action == "CLEAR":
        # delete stored images
        for p in user_files.get(uid, []):
            if os.path.exists(p):
                os.remove(p)
        user_files[uid] = []
        user_pdfname.pop(uid, None)
        user_await_name.discard(uid)

        await ensure_status(update, context, uid)
        await refresh_status(context, uid)

    elif action == "SETNAME":
        user_await_name.add(uid)
        await query.message.reply_text("📝 Send the PDF name now (example: `math_hw_week9`).", parse_mode="Markdown")
        await ensure_status(update, context, uid)
        await refresh_status(context, uid)

    elif action == "MODE":
        cur = user_mode.get(uid, "merge")
        user_mode[uid] = "separate" if cur == "merge" else "merge"
        await ensure_status(update, context, uid)
        await refresh_status(context, uid)

    elif action == "QUALITY":
        cur = user_quality.get(uid, "high")
        user_quality[uid] = "compressed" if cur == "high" else "high"
        await ensure_status(update, context, uid)
        await refresh_status(context, uid)

    elif action == "ENHANCE":
        user_enhance[uid] = not user_enhance.get(uid, DEFAULT_ENHANCE)
        await ensure_status(update, context, uid)
        await refresh_status(context, uid)

    elif action == "DONE":
        paths = user_files.get(uid, [])
        if not paths:
            await query.message.reply_text("No images yet 📸")
            await ensure_status(update, context, uid)
            return

        mode = user_mode.get(uid, "merge")
        base = user_pdfname.get(uid, "photos")

        prepared_paths: List[str] = []
        tmp_files: List[str] = []

        try:
            # Prepare images (enhance + resize + JPEG quality)
            for i, src in enumerate(paths, start=1):
                tmp = f"{uid}_prep_{i}.jpg"
                tmp_files.append(tmp)
                prepared_paths.append(prepare_image_for_pdf(src, tmp, uid))

            if mode == "merge":
                pdf_filename = f"{base}.pdf"
                pdf_path = f"{uid}_{pdf_filename}"
                build_a4_pdf_from_prepared(prepared_paths, pdf_path, margin_mm=PDF_MARGIN_MM)
                await query.message.reply_document(open(pdf_path, "rb"), filename=pdf_filename)
                if os.path.exists(pdf_path):
                    os.remove(pdf_path)
            else:
                # separate PDFs
                for i, prep in enumerate(prepared_paths, start=1):
                    pdf_filename = f"{base}_{i}.pdf"
                    pdf_path = f"{uid}_{pdf_filename}"
                    build_a4_pdf_from_prepared([prep], pdf_path, margin_mm=PDF_MARGIN_MM)
                    await query.message.reply_document(open(pdf_path, "rb"), filename=pdf_filename)
                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)

        except Exception as e:
            await query.message.reply_text(f"❌ Failed to create PDF: {e}")
        finally:
            # cleanup original downloads
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)
            # cleanup prepared temp files
            for t in tmp_files:
                if os.path.exists(t):
                    os.remove(t)

            user_files[uid] = []
            user_pdfname.pop(uid, None)
            user_await_name.discard(uid)

            await ensure_status(update, context, uid)
            await refresh_status(context, uid)


def main():
    if not TOKEN or TOKEN == "8397296110:AAGtA2awgyB3K90TOMJEx4f46f6zoWah230":
        raise RuntimeError("Set BOT_TOKEN env var or paste your real token into TOKEN.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))

    app.add_handler(CallbackQueryHandler(on_button))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))

    # name input after pressing Set Name
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_text))

    print("Bot running... Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
