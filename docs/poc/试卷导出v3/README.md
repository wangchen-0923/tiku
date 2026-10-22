# 试卷导出 v3.1 · POC 样张（2026-10-18）

A4 纵向样卷，**4 页**：20 道选择题（短/中/长选项，含手动 2 列折行）、
5 道判断题、3 道填空题、3 道简答题、2 张配图、3 个空白块、
5 项考生信息两端对齐、分组汇总。

## 文件

| 文件 | 说明 |
| --- | --- |
| `样卷-A4.pdf` | 试卷 PDF（按排版 JSON 坐标画） |
| `样卷-A4.docx` | 可编辑 Word（段落 + 选项表格 + inline 图片 + 页码域） |
| `样卷-答案.pdf` | 独立答案 PDF（不含题干/解析） |
| `layout.json` | 预览 / PDF / Word 共用排版 JSON |
| `page-01.png` … `page-04.png` | 300dpi 试卷页面截图 |
| `answer-01.png` | 300dpi 答案 PDF 截图 |
| `文字坐标.txt` | `diag_pdf_text_pos.py` 输出 |
| `一致性核对.txt` | `diag_paper_layout_consistency.py` 输出 |
| `Word校验.txt` | python-docx + Microsoft Word COM 真机打开/编辑 |
| `前端导出页-汇总.png` | Edge headless 页面截图 |
| `导出告警.txt` | 样张告警（手动 2 列长选项折行） |

## 一致性结果

```
文字基线：147 条，命中 147，最大偏差 0.000mm
选项 x  ：84 条，最大偏差 0.000mm
配图    ：2 张，JSON 50.80×38.10mm / PDF 50.80×38.10mm，差 0.00mm
空白块  ：3 个，偏差 0.00mm
结论    ：PASS —— 预览 JSON 与 PDF 是同一份几何
```

## Word 可编辑性

- 页面 210×297mm、四边 20mm；
- 段落 71、选项表格 20、inline 图片 2（段前/段后均为 0）；
- 中文字体 `w:eastAsia`：宋体 / 微软雅黑；字号 9–16pt 可直接改；
- 页脚含 `PAGE` / `NUMPAGES` 域；
- Microsoft Word COM 真机打开成功（Word 5 页 / PDF 4 页，流式差异）；
  编辑标题文字 + 字号 14pt 后页数不变，表格/图片保留。

## 复现

```powershell
$py = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PYTHONPATH = "D:\tiku\.devdeps;D:\tiku\server"
$env:ZF_DB_PORT = "15433"; $env:ZF_DB_PASSWORD = "zhifou"
& $py scripts/gen_poc_paper_export.py
```

★ 临时卷 `__POC_试卷导出v3__`、临时简答题、临时配图在 `finally` 删除。
