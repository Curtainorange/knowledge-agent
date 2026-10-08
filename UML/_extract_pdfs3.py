import pypdf, os

srcs = [
    (r"C:\Users\Curtain\Desktop\第3课_用例模型与Spec驱动的AI建模_v2.1(2).pdf",
     r"D:\知识学习智能体\UML\_第3课_原文.txt"),
]

for src, out in srcs:
    reader = pypdf.PdfReader(src)
    lines = ["# 课件原文提取（共 %d 页）\n" % len(reader.pages)]
    for i, page in enumerate(reader.pages, 1):
        txt = page.extract_text() or ""
        lines.append("\n===== PDF第%d页 =====\n" % i)
        lines.append(txt)
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("written:", out, "bytes:", os.path.getsize(out), "pages:", len(reader.pages))
