# 戳个印 Bug 修复实施计划

> 执行方式：subagent-driven-development；laborer 单项实施，主 Agent 复核、验证后勾选并提交。不得并发修改同一模块。

**Goal:** 为当前程序建立 Git 基线并修复已确认的六项缺陷。
**Architecture:** 保留 Qt 主窗口与核心引擎；图片导入统一规范化标识；导出改为后台任务，界面只接收进度与结果。保持 PDF pt 坐标合同及空白启动。
**Tech Stack:** Python 3.13、PySide6、PyMuPDF、PyPDF2、reportlab、Pillow、unittest。
**Spec:** 本计划依据用户确认的当前六项 Bug 清单；不恢复已删除组件，不改用户样本，不发布或推送远端。

## 决策与验收规则
- 用户已要求写完计划直接实施，无需另行确认。
- 同名结果采用递增后缀；任何已存在目标（包含源文件）不得覆盖。
- 首页布局明确为“全部文档页面”，以显式“应用到全部页面”操作生效，避免调参时意外覆盖单页编辑；水印开关保留；对齐默认“保留当前位置”，选择其他对齐才重设位置。透明度仍为“仅预览”。
- 图片身份采用 normcase(realpath(abspath(path)))，显示路径保留；删除后重映射所有页的图片索引。
- 后台导出使用输入快照，导出期间禁用编辑与重复导出，支持取消，在文件边界安全停止；关闭窗口不得销毁运行中的线程。
- 每项先写可失败的回归用例，再实现，主 Agent 运行测试后在下方标记 [x]，并单独提交。
- 运行解释器：D:\pyprojects\miniconda\python.exe；Qt 使用 QT_QPA_PLATFORM=offscreen。

## 进度
- [x] G0 初始化本地 Git，保存当前源码基线（bc5a91e）。构建产物、用户根目录样本和本机配置不纳入版本控制。
- [x] T1 修复图片重复添加。验证：3项回归通过；相对/大小写别名与两入口均去重，保留顺序。
- [x] T2 修复删除图片后的偏移索引错配。验证：5项回归通过，多文件多页删除A/C后保留B位置，空选择不变。
- [ ] T3 修复同名导出覆盖。
- [ ] T4 使首页布局参数有效且作用范围明确。
- [ ] T5 后台导出、取消及安全线程生命周期。
- [ ] T6 保留导出失败详情及明确最终状态。
- [ ] V 最终回归、代码审查和 Git 工作区检查。

## T1 图片去重
修改 pdf_watermark_demo.py；新增 tests/test_regressions.py。两种导入入口共享规范化路径身份。
回归：连续调用 _import_paths 导入同一图片两次，断言 len(_images)==1；mock 文件选择器再次添加同一文件也保持1。
运行：python -m unittest discover -s tests -v。先确认重复数量断言失败，修复集合中 Path/str 混用后重跑。

## T2 删除索引
修改 pdf_watermark_demo.py 和 tests/test_regressions.py。
回归：A/B/C，offsets={0:(1,2),1:(3,4),2:(5,6)}，删除A/C后B为索引0且值(3,4)，遍历多文件多页，未显式偏移仍回落dx/dy。允许多项删除；保留水印开关及尺寸。
实现 old_index -> new_index 映射，清除被删图偏移，更新每页 image_offsets。运行全部回归。

## T3 导出不覆盖
修改 pdf_watermark_demo.py，必要时新增 pdf_watermark_studio/core/export_jobs.py，新增相应测试。
回归：两个不同目录的 x.pdf 及已存在 watermarked_x.pdf，输出须各自唯一，原文件字节不变；重复导出仍不覆盖。测试只用临时目录。
实现检查已存在及本批预留路径，选择 watermarked_x.pdf、watermarked_x_2.pdf 等。真实导出失败也不能破坏既有文件；避免竞态使用独占预留或等价安全机制。

## T4 首页布局
修改 pdf_watermark_demo.py、必要的引擎接口和回归测试；保留单页cm/原始尺寸和旋转页换算。
首页增加明确应用按钮、范围说明；spin/slider双向同步(1..200)，固定宽度提供正确单位控件；尺寸通过 PageState 与 page_layout 贯穿主卡片、编辑页快速合成及导出；对齐仅在显式应用时按显示方向页尺寸及各图尺寸换算为中心相对pt偏移，禁止改变dx/dy的中心坐标合同。对齐默认“保留当前位置”；其他选项会重设每页每图位置，界面明确提示。opacity只影响预览且标签同步。所有构造页面副本处保留新增字段。
回归：应用前单页状态不变；应用后所有页得到所选尺寸；默认对齐时wm/dx/dy/image_offsets保持，选择非默认对齐后wm保持且每图的位置正确；透明度预览像素改变但导出不改变。运行回归和_verify_align.py，扩展至少非居中锚点用例。

## T5 后台导出
修改 pdf_watermark_demo.py、可新增 export_jobs.py、tests/test_regressions.py及_demo_smoke4.py适配等待后台结束。
回归：使用可控慢导出，Qt定时器在任务中仍执行；防止第二次启动/编辑；取消后余下文件不处理；任务中请求关闭不导致QThread销毁崩溃；成功后恢复控件。
任务持有独立输入快照，通过Qt信号逐页/逐文件报进度；不在工作线程操作控件。关闭时请求取消并等待异步完成再关闭，不强杀线程。

## T6 导出结果
修改 pdf_watermark_demo.py、导出任务结果接口、回归测试。
回归：三文件成功/失败/成功，最终显示成功2失败1，保留失败文件名和异常文本；取消状态独立，不显示全成功；再次导出会重置上次结果。
提供常驻只读详情区域或可重新打开的结果详情按钮；正常结果显示实际输出路径，失败不会被最终汇总覆盖。

## 最终验证
运行 python -m unittest discover -s tests -v、python _demo_smoke4.py、python _verify_align.py；离屏渲染主窗口和页面编辑器检查新增控件；git diff --check；审查整个修复范围。
每项提交消息包含对应T编号；仅当测试通过及复核完成才勾选。最终保留计划作为完成记录，不自动推送、重新打包或删除样本。
