"""Generate assets/amber.ico from the source art (assets/amber.png)."""
from pathlib import Path
from PIL import Image

assets = Path(__file__).resolve().parent / "assets"
src = Image.open(assets / "amber.png").convert("RGBA")

# Square-crop centred, just in case the source isn't perfectly square.
w, h = src.size
if w != h:
    s = min(w, h)
    src = src.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))

src.save(assets / "amber.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
src.resize((256, 256), Image.LANCZOS).save(assets / "amber.png")
print("wrote", assets / "amber.ico")
