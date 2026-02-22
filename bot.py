import os
import re

from PIL import Image, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN") or "8397296110:AAGtA2awgyB3K90TOMJEx4f46f6zoWah230"

# storage
user_files: dict[int, list[str]] = {}  # {uid: [img_paths]}
user_pdfname: dict[int, str] = {}  # {uid: "name_without_ext"}
user_await_name: set[int] = set()  # users waiting to type a name
user_mode: dict[int, str] = {}  # {uid: "merge" | "separate"}


# ---------- UI ----------
def kb(uid: int) -> InlineKeyboardMarkup:
    mode = user_mode.get(uid, "merge")
    mode_label = "Mode: Merge 📄" if mode == "merge" else "Mode: Separate 📑"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Done ✅", callback_data="DONE"),
                InlineKeyboardButton("Clear 🧹", callback_data="CLEAR"),
            ],
            [
                InlineKeyboardButton("Set Name 📝", callback_data="SETNAME"),
                InlineKeyboardButton(f"{mode_label} 🔁", callback_data="MODE"),
            ],
        ]
    )


def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)  # Windows illegal chars
    name = re.sub(r"\s+", " ", name)
    return name[:60] if name else "photos"


# ---------- PDF ----------
def build_a4_pdf(image_paths: list[str], out_pdf: str, margin_mm: float = 12.0):
    page_w, page_h = A4
    margin = margin_mm * mm
    avail_w = page_w - 2 * margin
    avail_h = page_h - 2 * margin

    c = canvas.Canvas(out_pdf, pagesize=A4)

    for path in image_paths:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)  # fix rotation
        img = img.convert("RGB")

        img_w, img_h = img.size
        scale = min(avail_w / img_w, avail_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale

        x = (page_w - draw_w) / 2
        y = (page_h - draw_h) / 2

        c.drawImage(
            ImageReader(img),
            x,
            y,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
            mask="auto",
        )
        c.showPage()

    c.save()


def build_single_a4_pdf(image_path: str, out_pdf: str, margin_mm: float = 12.0):
    build_a4_pdf([image_path], out_pdf, margin_mm=margin_mm)


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not uid:
        return

    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")

    text = (
        "📄 *Photo → PDF Bot*\n\n"
        "Send images as:\n"
        "• *Photo* (okay)\n"
        "• *File/Document* (HD)\n\n"
        "Then use buttons below."
    )
    if update.message:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb(uid)
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")

    photos = update.message.photo
    if not photos:
        return

    photo = photos[-1]  # best available size in "photo" message
    tg_file = await photo.get_file()

    img_path = f"{uid}_{update.message.message_id}_photo.jpg"
    await tg_file.download_to_drive(img_path)
    user_files[uid].append(img_path)

    await update.message.reply_text(
        f"✅ Added ({len(user_files[uid])})",
        reply_markup=kb(uid),
    )


async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")

    doc = update.message.document
    if not doc:
        return

    if not (doc.mime_type and doc.mime_type.startswith("image/")):
        await update.message.reply_text(
            "Please send an *image file* (JPG/PNG) as Document.", parse_mode="Markdown"
        )
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

    await update.message.reply_text(
        f"✅ Added HD ({len(user_files[uid])})",
        reply_markup=kb(uid),
    )


async def handle_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User types a name after pressing Set Name 📝"""
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    if uid not in user_await_name:
        return  # normal text ignored

    raw = (update.message.text or "").strip()
    fname = sanitize_filename(raw)
    user_pdfname[uid] = fname
    user_await_name.discard(uid)

    await update.message.reply_text(
        f"✅ PDF name set to: *{fname}.pdf*",
        parse_mode="Markdown",
        reply_markup=kb(uid),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return

    uid = query.from_user.id
    user_files.setdefault(uid, [])
    user_mode.setdefault(uid, "merge")

    await query.answer()

    action = query.data

    if action == "CLEAR":
        # delete stored files
        for path in user_files.get(uid, []):
            if os.path.exists(path):
                os.remove(path)
        user_files[uid] = []
        user_pdfname.pop(uid, None)
        user_await_name.discard(uid)

        await query.message.reply_text(
            "🧹 Cleared! Send new images.", reply_markup=kb(uid)
        )

    elif action == "SETNAME":
        user_await_name.add(uid)
        await query.message.reply_text(
            "📝 Send me the PDF name now (example: `math_hw_week9`).\n"
            "I’ll add `.pdf` automatically.",
            parse_mode="Markdown",
            reply_markup=kb(uid),
        )

    elif action == "MODE":
        # toggle
        cur = user_mode.get(uid, "merge")
        user_mode[uid] = "separate" if cur == "merge" else "merge"
        await query.message.reply_text(
            f"🔁 Mode changed to: *{user_mode[uid].upper()}*",
            parse_mode="Markdown",
            reply_markup=kb(uid),
        )

    elif action == "DONE":
        paths = user_files.get(uid, [])
        if not paths:
            await query.message.reply_text("No images yet 📸", reply_markup=kb(uid))
            return

        mode = user_mode.get(uid, "merge")
        base = user_pdfname.get(uid, "photos")

        try:
            if mode == "merge":
                pdf_filename = f"{base}.pdf"
                pdf_path = f"{uid}_{pdf_filename}"
                build_a4_pdf(paths, pdf_path, margin_mm=12.0)
                await query.message.reply_document(
                    open(pdf_path, "rb"), filename=pdf_filename
                )

                if os.path.exists(pdf_path):
                    os.remove(pdf_path)

            else:
                # separate: one pdf per image
                for i, img_path in enumerate(paths, start=1):
                    pdf_filename = f"{base}_{i}.pdf"
                    pdf_path = f"{uid}_{pdf_filename}"
                    build_single_a4_pdf(img_path, pdf_path, margin_mm=12.0)
                    await query.message.reply_document(
                        open(pdf_path, "rb"), filename=pdf_filename
                    )

                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)

        except Exception as e:
            await query.message.reply_text(
                f"❌ Failed to create PDF: {e}", reply_markup=kb(uid)
            )
            return
        finally:
            # cleanup images always
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)
            user_files[uid] = []
            user_pdfname.pop(uid, None)
            user_await_name.discard(uid)

        await query.message.reply_text(
            "✅ Done! Send more images anytime.", reply_markup=kb(uid)
        )


# ---------- main ----------
def main():
    if not TOKEN or TOKEN == "PASTE_YOUR_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env var or paste your real token into TOKEN.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Buttons
    app.add_handler(CallbackQueryHandler(on_button))

    # Images
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))

    # Name text (only used after pressing Set Name 📝)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_text))

    print("Bot running... Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
