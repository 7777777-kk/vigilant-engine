# 第三方项目与许可

以下项目通过安装其正式发布版接入。没有复制第三方项目的品牌、完整界面或示例图片。所有小程序页面和本项目的服务适配层为本次编写。

| 项目 | 实际版本 | 功能 | 上游与许可 |
| --- | --- | --- | --- |
| pdf2docx | 0.5.13 | PDF 转可编辑 DOCX，解析版式和表格 | https://github.com/ArtifexSoftware/pdf2docx · MIT |
| PyMuPDF | 1.28.2 | pdf2docx 的 PDF 解析依赖 | https://github.com/pymupdf/PyMuPDF · GNU AGPL v3 / 商业许可 |
| pypdf | 6.20.1 | 合并、页面提取、无损压缩、文字提取 | https://github.com/py-pdf/pypdf · BSD-3-Clause |
| pypdfium2 | 5.14.0 | PDF 页面渲染 | https://github.com/pypdfium2-team/pypdfium2 · Apache-2.0 / BSD-3-Clause；PDFium 及其依赖另有许可 |
| Pillow | 12.3.0 | 图片处理、图片合成 PDF | https://github.com/python-pillow/Pillow · HPND / MIT-CMU 等，见发行包许可 |
| python-docx | 1.2.0 | DOCX 验证及测试 | https://github.com/python-openxml/python-docx · MIT |
| FastAPI | 0.143.0 | API 服务 | https://github.com/fastapi/fastapi · MIT |
| LibreOffice | 运行环境提供 | DOCX 渲染为 PDF | https://www.libreoffice.org/ · MPL-2.0 / LGPL-3.0+ 等，见发行版许可 |
| RapidOCR ONNX Runtime | 1.4.4 | 本地中英文扫描文字识别，含检测、方向分类和识别模型 | https://github.com/RapidAI/RapidOCR · Apache-2.0 |
| ONNX Runtime | 1.23.2 | OCR 模型 CPU 推理 | https://github.com/microsoft/onnxruntime · MIT |
| PaddleOCR PP-OCR 模型 | RapidOCR 1.4.4 内置版本 | 中英文检测和识别模型来源 | https://github.com/PaddlePaddle/PaddleOCR · Apache-2.0；内置模型哈希见 third_party/licenses/PaddleOCR/models.json |
| OpenCV | 5.0.0.93 | 图像处理、带边框表格网格检测 | https://github.com/opencv/opencv · Apache-2.0；发行包第三方许可随包保留 |

测试与工具依赖包含 uvicorn、httpx、pytest、ReportLab、Playwright 和 miniprogram-compiler，完整锁定版本见 `server/requirements.lock.txt` 与 `package-lock.json`。

pdf2docx 上游已说明 Artifex 不再主动维护此项目。本项目锁定已验证版本，升级时应重新运行中文、表格与分页测试。

注意：pdf2docx 本身的 MIT 许可不会改变其 PyMuPDF 依赖的 AGPL / 商业许可条件。分发或部署带有该引擎的服务时，须按实际依赖许可证安排使用方式；不能将整个依赖栈都标记为 MIT。许可证原文已随源码保留，PyMuPDF 的说明以其 COPYING 文件为准。

曾评估完整产品 [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF)。其功能丰富，但当前工程选择直接接入上述转换引擎，减少对大型 Java / Docker 应用的运行依赖。**没有将 Stirling-PDF 的代码或服务作为已集成功能宣称。**
