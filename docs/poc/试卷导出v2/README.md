# 试卷导出 v2 · POC 样张（2026-10-18）

A4 纵向样卷：20 道选择题 + 3 道简答题、**2 张配图**、**3 个空白块**、
长注意事项、`（　　）` 题号、页码居中。共 **4 页**。

## 文件

| 文件 | 说明 |
| --- | --- |
| `样卷-A4-2图-3空白.pdf` | 打印版 PDF（全部按排版 JSON 坐标画） |
| `样卷-A4-2图-3空白.docx` | 可编辑 Word（段落 + 表格 + inline 图片 + 页码域） |
| `layout.json` | 预览 / PDF / Word 共用的排版 JSON |
| `page-01.png` … `page-04.png` | 300dpi 页面截图 |
| `文字坐标.txt` | `scripts/diag_pdf_text_pos.py` 输出（x / bbox 顶 / 基线 / 宽） |
| `一致性核对.txt` | `scripts/diag_paper_layout_consistency.py` 输出 |
| `Word校验.txt` | python-docx 读回的页面/字体/图片/页码域核对 |
| `前端导出页.png` | Edge headless 打开 `/papers/193/export` 的截图 |
| `导出告警.txt` | 本次样张的告警（空 = 无降级/缺图） |

## 一致性结果

```
文字基线：59 条，命中 59，最大偏差 0.000mm
配图    ：2 张，JSON 50.80×38.10mm / PDF 50.80×38.10mm，差 0.00/0.00mm
空白块  ：3 个，高 35 / 30 / 45mm；后续文字偏差 0.00mm
结论    ：PASS —— 预览 JSON 与 PDF 是同一份几何
```

## Word 可编辑性

- 页面 210×297mm、四边 20mm；
- 51 段、40 张无边框表格、2 张 inline picture；
- 中文 `w:eastAsia` 字体：宋体 / 微软雅黑；
- 字号 9 / 9.5 / 10.5 / 12 / 16pt，可直接改；
- 页脚含 `PAGE` / `NUMPAGES` 域（打印或更新域后显示）。

## Word 真机打开 / 编辑

用 Microsoft Word COM 打开同一份 DOCX（副本）：

- `Documents.Open` 成功，未触发修复/只读；Word 分页 **5 页**（PDF 4 页，流式差异属已知限制）；
- 在标题段插入文字并把整段字号改为 14pt，保存后读回文字/字号均生效；
- 编辑前后页数都是 5，表格行 40、图片 2 均保留 → **版式未崩**；
- 验证副本已删除，交付样张未改动。详见 `Word校验.txt`。

## 复现

```powershell
$py = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "D:\tiku\.devdeps;D:\tiku\server"
$env:ZF_DB_PORT = "15433"; $env:ZF_DB_PASSWORD = "zhifou"
& $py scripts/gen_poc_paper_export.py
```

★ 脚本创建的临时卷 `__POC_试卷导出v2__` 与临时配图在 `finally` 里删除，
不污染真实数据。
