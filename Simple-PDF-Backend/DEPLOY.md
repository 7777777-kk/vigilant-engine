# Simple PDF 转换后端部署

这是微信云托管的后端源码包。解压后将包内文件和目录上传至 GitHub 仓库根目录。

根目录结构：Dockerfile、.dockerignore、server/、third_party/、THIRD_PARTY_NOTICES.md。

微信云托管配置：
- 服务名称：simple-pdf
- 分支：main
- 服务端口：8765
- 构建上下文：仓库根目录（界面需要填写时使用 . 或 /，依其字段说明）
- Dockerfile：Dockerfile
- 环境变量：初次部署不需要提供密钥
- 健康检查路径：/api/health（界面有该项时填写）

Dockerfile 已安装 LibreOffice Writer 和中文字体，并通过 uvicorn 启动 FastAPI。
使用单个应用 worker。当前后端任务与暂存文件按单实例设计；初始联调应保持单实例。
弹性扩缩容和长期公用运行前，需要接入外部文件存储及共享任务状态。

部署成功后还需接入小程序云调用及文件传输。本包部署成功不等同于小程序已发布。
本机已验证 PDF 转 Word、图片、文字、合并、提取、压缩和图片转 PDF。
当前本机没有 Docker / LibreOffice，Linux 镜像构建及 Word 转 PDF 要通过云端联调确认。

许可证及已安装版本见 THIRD_PARTY_NOTICES.md 与 third_party/licenses/。
