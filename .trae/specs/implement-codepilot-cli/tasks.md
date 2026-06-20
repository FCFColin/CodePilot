# Tasks

## Phase 1: 基础骨架
- [x] Task 1: 创建项目结构与空文件
  - [x] SubTask 1.1: 创建 `codepilot/` 目录及所有子目录（providers/tools/security/context/ui/agent）
  - [x] SubTask 1.2: 创建所有 `__init__.py` 与模块空文件（main.py、config.py 及各模块文件）
  - [x] SubTask 1.3: 创建 `requirements.txt`、`.codepilot.yml.example`
- [x] Task 2: 实现配置加载（config.py）
  - [x] SubTask 2.1: 定义配置数据结构（provider/deepseek/anthropic/security/context/ui 段）
  - [x] SubTask 2.2: 实现多级配置加载（CLI 参数 > 环境变量 > .codepilot.yml > 用户目录 > 默认值）
  - [x] SubTask 2.3: 实现 `${ENV_VAR}` 环境变量引用替换
  - [x] SubTask 2.4: workspace_root 解析为绝对路径
- [x] Task 3: 实现启动 banner 与基本 REPL 循环
  - [x] SubTask 3.1: 实现 `ui/banner.py` ASCII art banner（含版本、provider、workspace、安全状态、命令列表）
  - [x] SubTask 3.2: 实现 `main.py` argparse 参数解析（交互模式/单次模式/各参数）
  - [x] SubTask 3.3: 实现 REPL 循环骨架（读取输入、识别 slash 命令、/quit 退出）
- [x] Task 4: Phase 1 验收
  - [x] SubTask 4.1: 运行 `python main.py` 能看到 banner，能输入文字，`/quit` 退出

## Phase 2: Provider 适配
- [x] Task 5: 实现 providers/base.py 抽象接口
  - [x] SubTask 5.1: 定义 `AgentEvent` 类型（TextDelta/ThinkingDelta/ToolCall/Usage/Done）
  - [x] SubTask 5.2: 定义 `BaseProvider` 抽象基类（`async chat` 方法）
  - [x] SubTask 5.3: 定义 `Message`、`ToolCallResult` 数据类
- [x] Task 6: 实现 providers/deepseek.py
  - [x] SubTask 6.1: 使用 openai SDK，base_url 设为 deepseek
  - [x] SubTask 6.2: 实现流式响应解析为 AgentEvent
  - [x] SubTask 6.3: 实现 tool_calls 解析（arguments JSON 字符串 parse，按 index 累积）
  - [x] SubTask 6.4: 支持 thinking 模式（reasoning_content 字段，extra_body 传递）
  - [x] SubTask 6.5: 实现 format_tool_result（OpenAI tool 消息格式）
- [x] Task 7: 实现 providers/anthropic.py
  - [x] SubTask 7.1: 使用 anthropic SDK 原生 Messages API
  - [x] SubTask 7.2: 实现流式响应解析为 AgentEvent
  - [x] SubTask 7.3: 实现 tool_use block 解析（input_json_delta 累积后 parse）
  - [x] SubTask 7.4: 实现 tool_result 回传格式（role=user + tool_result block）
- [x] Task 8: Phase 2 验收
  - [x] SubTask 8.1: 三个模块 import 无误，无 API key 可实例化
  - [x] SubTask 8.2: 能发送简单消息并流式显示响应（需 API key 实测）

## Phase 3: 工具系统
- [x] Task 9: 实现 tools/registry.py 工具注册表
  - [x] SubTask 9.1: 定义工具抽象接口（name/description/parameters/execute）
  - [x] SubTask 9.2: 实现注册表，支持转换为 DeepSeek 和 Anthropic 两种格式
- [x] Task 10: 实现 7 个核心工具
  - [x] SubTask 10.1: `read_file`（带行号、100KB 截断、UTF-8、跳过二进制）
  - [x] SubTask 10.2: `write_file`（创建/覆写、diff 预览、需确认）
  - [x] SubTask 10.3: `edit_file`（搜索替换、需确认）
  - [x] SubTask 10.4: `list_files`（递归目录树、深度/过滤配置）
  - [x] SubTask 10.5: `shell_exec`（cwd=workspace、30s 超时、200 行截断、禁交互式）
  - [x] SubTask 10.6: `search_code`（grep 风格、正则支持）
  - [x] SubTask 10.7: `get_context`（返回上下文统计）
- [x] Task 11: Phase 3 验收
  - [x] SubTask 11.1: 发送 "list all files in current directory" 能触发 list_files 工具并显示结果

## Phase 4: Agent 循环
- [x] Task 12: 实现 agent/loop.py 核心循环
  - [x] SubTask 12.1: 实现用户输入→context→provider.chat→流式输出主流程
  - [x] SubTask 12.2: 实现 tool_calls 处理（UI 显示→执行→结果回传→继续循环）
  - [x] SubTask 12.3: 实现单轮 25 次工具调用上限
  - [x] SubTask 12.4: 实现工具失败错误回传
  - [x] SubTask 12.5: 实现每次工具调用后上下文压缩检查
- [x] Task 13: Phase 4 验收
  - [x] SubTask 13.1: 能完成多步任务，如 "read main.py and add error handling"

## Phase 5: 安全系统
- [x] Task 14: 实现 security/sandbox.py
  - [x] SubTask 14.1: 实现 `validate_path`（resolve、遍历检查、blocked_paths、workspace/allowed_dirs、敏感文件、符号链接）
  - [x] SubTask 14.2: 实现 `validate_command`（链式拆解、黑名单、白名单、sudo/交互式禁用）
- [x] Task 15: 实现 security/command_filter.py
  - [x] SubTask 15.1: 黑名单模式匹配
  - [x] SubTask 15.2: 白名单模式前缀匹配
- [x] Task 16: 实现 security/approval.py
  - [x] SubTask 16.1: 实现审批面板（diff 预览、命令高亮）
  - [x] SubTask 16.2: 实现 y/n/a/! 四种选择
  - [x] SubTask 16.3: 实现本会话自动批准与 YOLO 模式
- [x] Task 17: Phase 5 验收
  - [x] SubTask 17.1: 尝试读取 /etc/passwd 被拒绝
  - [x] SubTask 17.2: 写文件时显示 diff 并请求确认

## Phase 6: 上下文管理
- [x] Task 18: 实现 context/token_counter.py
  - [x] SubTask 18.1: tiktoken cl100k_base 精确计数
  - [x] SubTask 18.2: 字符数/3.5 估算回退
  - [x] SubTask 18.3: 消息级 token 缓存
- [x] Task 19: 实现 context/manager.py
  - [x] SubTask 19.1: 管理 system_prompt、messages、compressed_summary、total_tokens
  - [x] SubTask 19.2: 实现 add_message/get_context/force_compress/get_stats
  - [x] SubTask 19.3: 实现压缩触发逻辑（threshold/critical_threshold）
  - [x] SubTask 19.4: 线程安全
- [x] Task 20: 实现 context/compressor.py
  - [x] SubTask 20.1: 实现 summary 策略（LLM 结构化摘要：CONTEXT/KEY ACTIONS/OUTCOMES/CURRENT STATE/IMPORTANT REFERENCES）
  - [x] SubTask 20.2: 实现 truncate 策略
  - [x] SubTask 20.3: 实现 hybrid 策略
  - [x] SubTask 20.4: 实现完整历史写入 history_file
  - [x] SubTask 20.5: 实现压缩通知显示
- [x] Task 21: Phase 6 验收
  - [x] SubTask 21.1: `/stats` 显示 token 统计
  - [x] SubTask 21.2: `/compact` 手动触发压缩

## Phase 7: 完善UI
- [x] Task 22: 完善 Rich 面板显示
  - [x] SubTask 22.1: 用户输入面板、工具调用面板、assistant 回复面板
  - [x] SubTask 22.2: token 用量/上下文占比/费用估算底部状态栏
  - [x] SubTask 22.3: 安全拒绝面板（SECURITY BLOCK）
  - [x] SubTask 22.4: 压缩通知面板
- [x] Task 23: 实现 diff 着色显示（ui/diff_view.py）
- [x] Task 24: 实现所有 slash 命令
  - [x] SubTask 24.1: /help、/config、/stats、/clear、/compact、/history
  - [x] SubTask 24.2: /model、/provider、/approve、/undo、/quit、/exit
- [x] Task 25: 错误处理与边界情况
  - [x] SubTask 25.1: 网络/API/工具错误优雅处理
  - [x] SubTask 25.2: Ctrl+C 中断当前 LLM 调用回到提示符
  - [x] SubTask 25.3: Ctrl+D 退出
- [x] Task 26: Phase 7 验收
  - [x] SubTask 26.1: 完整一轮交互（提问→工具调用→确认→结果→回答）所有 UI 元素正常显示

# Task Dependencies
- Task 2 依赖 Task 1
- Task 3 依赖 Task 2
- Task 5-7 依赖 Task 3
- Task 9-10 依赖 Task 5
- Task 12 依赖 Task 9、Task 5
- Task 14-16 依赖 Task 9
- Task 18-20 依赖 Task 5
- Task 22-25 依赖 Task 12、Task 14、Task 18
- Phase N 依赖 Phase N-1 完成
