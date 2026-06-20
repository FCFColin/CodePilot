# Checklist

## Phase 1: 基础骨架
- [x] `codepilot/` 目录结构完整，所有模块文件存在
- [x] `requirements.txt` 包含 openai、anthropic、rich、prompt_toolkit、pyyaml、tiktoken
- [x] `.codepilot.yml.example` 示例配置完整
- [x] `config.py` 能按优先级加载配置（CLI > 环境变量 > .codepilot.yml > 用户目录 > 默认值）
- [x] `${ENV_VAR}` 环境变量引用被正确替换
- [x] `workspace_root` 被解析为绝对路径
- [x] `ui/banner.py` 显示完整启动 banner（ASCII art、版本、provider、workspace、安全状态、命令列表）
- [x] `main.py` 支持 argparse 参数（交互模式、单次模式、--provider、--model、--api-key、--workspace、--no-approve、--config、--verbose、--version）
- [x] REPL 循环能读取输入、识别 slash 命令、`/quit` 退出
- [x] 运行 `python main.py` 能看到 banner 并交互

## Phase 2: Provider 适配
- [x] `providers/base.py` 定义 `AgentEvent` 五种类型（TextDelta/ThinkingDelta/ToolCall/Usage/Done）
- [x] `BaseProvider` 抽象基类定义 `async chat` 方法
- [x] `Message`、`ToolCallResult` 数据类定义
- [x] `providers/deepseek.py` 使用 openai SDK，base_url 为 deepseek
- [x] DeepSeek 流式响应正确解析为 AgentEvent
- [x] DeepSeek tool_calls 的 arguments JSON 字符串被正确 parse（按 index 累积）
- [x] DeepSeek 支持 thinking 模式（reasoning_content 字段，extra_body 传递）
- [x] DeepSeek format_tool_result 返回 OpenAI tool 消息格式
- [x] `providers/anthropic.py` 使用 anthropic SDK 原生 Messages API
- [x] Anthropic 流式响应正确解析为 AgentEvent
- [x] Anthropic tool_use block 的 input（input_json_delta 累积后 parse）被正确处理
- [x] Anthropic tool_result 以 `role=user` + `tool_result` block 回传
- [x] 三个模块 import 无误，无 API key 可实例化 provider
- [ ] 能发送简单消息并流式显示响应（需 API key 实测）

## Phase 3: 工具系统
- [x] `tools/registry.py` 定义工具抽象接口
- [x] 注册表能将工具定义转换为 DeepSeek（OpenAI function 格式）和 Anthropic（input_schema 格式）
- [x] `read_file` 带行号显示、100KB 截断、UTF-8 编码、跳过二进制
- [x] `write_file` 创建/覆写、显示 diff 预览、需确认
- [x] `edit_file` 搜索替换、需确认
- [x] `list_files` 递归目录树、支持深度和过滤配置
- [x] `shell_exec` 在 workspace 执行、30s 超时、200 行截断、禁交互式命令
- [x] `search_code` grep 风格、支持正则
- [x] `get_context` 返回上下文统计
- [x] 每个工具经过 sandbox 校验后执行
- [x] 危险操作经 approval 确认
- [x] 工具输出做截断处理
- [ ] 发送 "list all files" 能触发 list_files 并显示结果

## Phase 4: Agent 循环
- [x] `agent/loop.py` 实现用户输入→context→provider.chat→流式输出主流程
- [x] tool_calls 处理：UI 显示→安全校验→审批→执行→结果回传→继续循环
- [x] 单轮最多 25 次工具调用
- [x] 工具失败错误信息作为 tool_result 回传
- [x] 每次工具调用后检查上下文压缩
- [ ] 能完成多步任务（如 "read main.py and add error handling"）

## Phase 5: 安全系统
- [x] `security/sandbox.py` `validate_path` 实现：resolve、遍历检查、blocked_paths、workspace/allowed_dirs、敏感文件、符号链接
- [x] `validate_command` 实现：链式拆解、黑名单、白名单、sudo/交互式禁用
- [x] `security/command_filter.py` 黑名单模式匹配
- [x] 白名单模式前缀匹配
- [x] `security/approval.py` 审批面板（diff 预览、命令高亮）
- [x] 支持 y/n/a/! 四种选择
- [x] 本会话自动批准与 YOLO 模式
- [x] 尝试读取 /etc/passwd 被拒绝
- [x] 写文件时显示 diff 并请求确认

## Phase 6: 上下文管理
- [x] `context/token_counter.py` tiktoken cl100k_base 精确计数
- [x] 未安装 tiktoken 时字符数/3.5 估算回退
- [x] 消息级 token 缓存
- [x] `context/manager.py` 管理 system_prompt/messages/compressed_summary/total_tokens
- [x] add_message/get_context/force_compress/get_stats 方法实现
- [x] 压缩触发逻辑（compression_threshold 0.70 / critical_threshold 0.85）
- [x] 线程安全
- [x] `context/compressor.py` summary 策略（LLM 结构化摘要，保留路径/名称/错误/决策）
- [x] truncate 策略
- [x] hybrid 策略
- [x] 完整历史写入 history_file
- [x] 压缩通知显示（前→后 token、缩减比例）
- [x] `/stats` 显示 token 统计
- [x] `/compact` 手动触发压缩

## Phase 7: 完善UI
- [x] 用户输入面板、工具调用面板、assistant 回复面板
- [x] token 用量/上下文占比/费用估算底部状态栏
- [x] 安全拒绝面板（SECURITY BLOCK）
- [x] 压缩通知面板
- [x] `ui/diff_view.py` diff 着色显示
- [x] /help、/config、/stats、/clear、/compact、/history 命令
- [x] /model、/provider、/approve、/undo、/quit、/exit 命令
- [x] 网络/API/工具错误优雅处理，不 crash
- [x] Ctrl+C 中断当前 LLM 调用回到提示符
- [x] Ctrl+D 退出
- [ ] 完整一轮交互所有 UI 元素正常显示

## 关键实现约束
- [x] 所有文件 I/O 使用 async
- [x] LLM 响应流式显示
- [x] 错误优雅处理，不 crash
- [x] 文件读写统一 UTF-8，二进制跳过
- [x] 路径统一 `os.path.realpath()` 解析后比较
- [x] 读取文件超过 100KB 自动截断并告知 LLM
- [x] async 下 context_manager 线程安全
