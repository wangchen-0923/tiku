# OMR 定位/铅笔回归 fixtures

| 文件 | 尺寸 | 来源 | 用途 |
| --- | --- | --- | --- |
| `photo_1280_tilted.png` | 1280×1707 | 真实上传卡 25 原图（`server/.zf-storage/.../8d308d51...png`） | A4 打印、纸面倾斜的手机照片；旧实现按 300dpi 推期望锚点，候选恒为 0 |
| `photo_3024_pencil.png` | 3024×4032 | 真实上传卡 21 原图（`server/.zf-storage/.../551e8ea3...png`） | 灰色铅笔实拍；答案 `1C 2C 3A 4C 5C 6A 7B 8A 9B 10A`，学号 `24251417` |
| `a5_scaled.png` | 1754×2480 | 合成 A4 300dpi 卡整体缩放到 70.7% | A4 设计按 A5 打印；验证等比缩放不影响定位 |
| `layout_card_213.json` | — | 真实卡 21/25 使用的模板 213 布局，从数据库原样导出 | **模板坐标未做任何修改** |
| `layout_synthetic_15q.json` | — | 生成 `a5_scaled.png` 时使用的合成布局 | 与 A5 图配套，答案已知 |

`a5_scaled.png` 的答案：

```text
1A 2B 3C 4D 5A 6B 7A 8B 9A 10B 11A 12B 13A 14B 15A
学号 20265001
```

重新生成：

```powershell
python scripts/gen_omr_fixtures.py
```

## 期望文件（`*.expect.json`）

`scripts/omr_selfcheck.py` 按「同名一对」的约定取样本：`<name>.png` +
`<name>.expect.json`。本目录的三份期望文件是**手工登记**的（图不重新生成）：

| 样本 | 断言内容 |
| --- | --- |
| `a5_scaled` | 15 题答案全量 + 学号 `20265001` + `AUTO_OK`；验证 A4→A5 等比缩放不改锚点相对位置 |
| `photo_3024_pencil` | 客观题 10 题答案 + 学号 `24251417` + `AUTO_OK`；验证真实铅笔灰度的阈值 |
| `photo_1280_tilted` | 只断言定位与 `AUTO_OK`，**不断言逐题答案**（原卡答案未记录）；1280px 逼近低分辨率下限 |

字段说明：

```jsonc
{
  "layout_json": { ... },   // 该卡的模板布局（内联，样本自包含）
  "template_id": 213,       // 或者从库里取（需连库；本目录用内联）
  "student_no": "20265001", // 留空则不断言学号
  "answers": { "1": "A", "2": "BC" },  // 留空则不断言答案
  "status": "AUTO_OK",      // AUTO_OK / NEEDS_REVIEW / FAILED
  "note": "这张样本守的是什么"
}
```

★ 判定口径是**三者全中**才算通过（状态、学号、逐题答案）。
「答案对了但状态是 `NEEDS_REVIEW`」**不算通过**——它会被推进人工队列，
而样本库的意义正是守住「这张卡本来应该静默通过」。

跑自检：

```powershell
python scripts/omr_selfcheck.py --report reports/omr.html
```

★ 需要 `cv2` / `numpy` / `PyMuPDF`。缺这些包时脚本会直接说明缺什么，
不做静默跳过——能跑识别才谈得上跑回归。

★ 照片是原图直接拷贝，没有压缩、没有裁切；不要为了减小体积覆盖它们，
否则「真实铅笔灰度」这条回归就失去意义。
