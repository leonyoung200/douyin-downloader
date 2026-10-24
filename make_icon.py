"""生成抖快批量下载器的新品牌图标 app_icon.ico（多尺寸）。

设计：红(#fe2c55)→橙(#ff7a00) 对角渐变圆角磁贴 + 白色「向下箭头 + 托盘」下载符号。
"""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 512
R = 112  # 圆角半径


def diagonal_gradient(c1, c2):
    base = Image.new("RGBA", (S, S), c1)
    top = Image.new("RGBA", (S, S), c2)
    mask = Image.new("L", (S, S))
    mp = mask.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            mp[x, y] = max(0, min(255, int(t * 255)))
    return Image.composite(top, base, mask)


def rounded_alpha(radius):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=radius, fill=255)
    return m


def draw_glyph(im):
    d = ImageDraw.Draw(im)
    cx = S / 2
    white = (255, 255, 255, 255)
    # ---- 向下箭头 ----
    head_top = 150
    head_w = 150          # 箭头总宽
    head_y = 300          # 箭头底边（与杆相交处）
    tip_y = 372           # 箭头尖
    # 箭杆（竖直圆角矩形）
    shaft_w = 52
    d.rounded_rectangle(
        [cx - shaft_w / 2, head_top, cx + shaft_w / 2, head_y + 24],
        radius=shaft_w / 2, fill=white,
    )
    # 箭头头部（向下三角）
    d.polygon(
        [(cx - head_w / 2, head_y), (cx + head_w / 2, head_y), (cx, tip_y)],
        fill=white,
    )
    # ---- 托盘 ----
    tray_y = 392
    tray_h = 34
    tray_l = cx - 150
    tray_r = cx + 150
    d.rounded_rectangle([tray_l, tray_y, tray_r, tray_y + tray_h], radius=17, fill=white)
    # 托盘两端上翘（小竖条）
    foot_w = 34
    foot_h = 40
    d.rounded_rectangle([tray_l, tray_y - foot_h, tray_l + foot_w, tray_y + 4], radius=15, fill=white)
    d.rounded_rectangle([tray_r - foot_w, tray_y - foot_h, tray_r, tray_y + 4], radius=15, fill=white)
    return im


# 抗锯齿：4x 超采样
SS = 4
big = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
bd = ImageDraw.Draw(big)
# 渐变（大图）
base = Image.new("RGBA", (S * SS, S * SS), (0xFE, 0x2C, 0x55, 255))
top = Image.new("RGBA", (S * SS, S * SS), (0xFF, 0x7A, 0x00, 255))
mask = Image.new("L", (S * SS, S * SS))
mp = mask.load()
for y in range(S * SS):
    for x in range(S * SS):
        t = (x + y) / (2 * S * SS)
        mp[x, y] = max(0, min(255, int(t * 255)))
tile = Image.composite(top, base, mask)
# 圆角
amask = Image.new("L", (S * SS, S * SS), 0)
ImageDraw.Draw(amask).rounded_rectangle(
    [0, 0, S * SS - 1, S * SS - 1], radius=R * SS, fill=255)
tile.putalpha(amask)
big.paste(tile, (0, 0), tile)

# 画布（含透明）：渐变磁贴 + 白色符号
glyph = Image.new("RGBA", (S * SS, S * SS), (0, 0, 0, 0))
gd = ImageDraw.Draw(glyph)
cx = S * SS / 2
white = (255, 255, 255, 255)
# 箭头
hw = 150 * SS
hy = 300 * SS
ty = 372 * SS
ht = 150 * SS
sw = 52 * SS
gd.rounded_rectangle([cx - sw / 2, ht, cx + sw / 2, hy + 24 * SS], radius=sw / 2, fill=white)
gd.polygon([(cx - hw / 2, hy), (cx + hw / 2, hy), (cx, ty)], fill=white)
# 托盘
tray_y = 396 * SS
tray_h = 30 * SS
tray_l = cx - 148 * SS
tray_r = cx + 148 * SS
gd.rounded_rectangle([tray_l, tray_y, tray_r, tray_y + tray_h], radius=15 * SS, fill=white)
fw = 30 * SS
fh = 30 * SS
gd.rounded_rectangle([tray_l - 2 * SS, tray_y - fh, tray_l + fw - 2 * SS, tray_y + 2 * SS], radius=14 * SS, fill=white)
gd.rounded_rectangle([tray_r - fw + 2 * SS, tray_y - fh, tray_r + 2 * SS, tray_y + 2 * SS], radius=14 * SS, fill=white)
glyph = glyph.filter(ImageFilter.GaussianBlur(0.8 * SS))

# 合成（磁贴 + 符号），并把符号叠加在磁贴上
big = Image.alpha_composite(big, glyph)
# 高斯轻模糊整体以柔化（已在 glyph 上做）

master = big.resize((S, S), Image.LANCZOS)
master.save("app_icon_preview.png")

# 多尺寸 ICO
sizes = [16, 24, 32, 48, 64, 128, 256]
frames = []
for sz in sizes:
    frames.append(master.resize((sz, sz), Image.LANCZOS))
master.save("app_icon.ico", sizes=[(s, s) for s in sizes])
print("OK: app_icon.ico sizes", sizes)
