 编码智能体CLI构建指令
项目概述
请构建一个名为 CodePilot 的终端编码智能体CLI工具，使用 Python 3.11+ 开发。该工具是一个交互式终端助手，能够读写文件、执行终端命令、管理项目代码，同时具备严格的安全沙箱限制和自动上下文压缩能力。需要支持 DeepSeek 官方 API（OpenAI 兼容格式）和 Anthropic Claude API 两种后端。

一、项目结构
text

codepilot/
├── main.py                   # 入口文件，CLI启动
├── config.py                 # 配置加载（YAML/ENV/.codepilot.yml）
├── providers/
│   ├── __init__.py
│   ├── base.py               # 抽象Provider基类
│   ├── deepseek.py           # DeepSeek API适配器（OpenAI兼容格式）
│   └── anthropic.py          # Anthropic Claude API适配器（原生Messages API）
├── tools/
│   ├── __init__.py
│   ├── file_read.py          # 读取文件工具
│   ├── file_write.py         # 写入文件工具（含diff预览）
│   ├── file_edit.py          # 搜索替换编辑工具
│   ├── list_files.py         # 列出目录文件树
│   ├── shell_exec.py         # 终端命令执行工具
│   ├── search_code.py        # 代码搜索（grep）工具
│   └── registry.py           # 工具注册表，统一管理所有工具
├── security/
│   ├── __init__.py
│   ├── sandbox.py            # 目录沙箱 & 路径校验
│   ├── command_filter.py     # 命令黑白名单过滤
│   └── approval.py           # 人工审批流程（危险操作确认）
├── context/
│   ├── __init__.py
│   ├── manager.py            # 上下文管理器（消息历史）
│   ├── compressor.py         # 自动上下文压缩器
│   └── token_counter.py      # Token计数器（tiktoken / 字符估算）
├── ui/
│   ├── __init__.py
│   ├── display.py            # Rich终端UI渲染（状态栏、面板、spinner）
│   ├── diff_view.py          # 文件变更diff着色显示
│   └── banner.py             # 启动banner & 状态信息
├── agent/
│   ├── __init__.py
│   └── loop.py               # 核心Agent循环（tool_use agentic loop）
├── requirements.txt
├── .codepilot.yml.example    # 示例配置
└── README.md
二、配置系统详细设计
2.1 配置文件格式 .codepilot.yml
YAML

# ===== Provider 配置 =====
provider: "deepseek"  # 可选: "deepseek" | "anthropic"

deepseek:
  api_key: "${DEEPSEEK_API_KEY}"     # 支持环境变量引用
  base_url: "https://api.deepseek.com"
  model: "deepseek-chat"             # 或 deepseek-reasoner / deepseek-v4-pro / deepseek-v4-flash
  max_tokens: 8192
  temperature: 1.0                   # DeepSeek推荐 temperature=1.0, top_p=1.0
  top_p: 1.0
  stream: true
  thinking:                          # 深度思考模式（可选）
    enabled: false
    reasoning_effort: "high"         # high | max

anthropic:
  api_key: "${ANTHROPIC_API_KEY}"
  base_url: "https://api.anthropic.com"   # 也支持Anthropic兼容的第三方接口
  model: "claude-sonnet-4-20250514"
  max_tokens: 8192
  temperature: 0.7

# ===== 安全配置 =====
security:
  workspace_root: "."                # 允许操作的工作区根目录（自动解析为绝对路径）
  allowed_dirs:                      # 额外允许访问的目录（可选）
    - "/tmp/codepilot"
  blocked_paths:                     # 绝对禁止访问的路径
    - "/"
    - "/etc"
    - "/usr"
    - "/var"
    - "/sys"
    - "/proc"
    - "/boot"
    - "/root"
    - "~"                            # 用户home目录也默认禁止（除非在workspace内）
  command_blacklist:                  # 禁止执行的命令模式
    - "rm -rf /"
    - "rm -rf ~"
    - "rm -rf /*"
    - "mkfs"
    - "dd if="
    - ":(){:|:&};:"
    - "chmod -R 777 /"
    - "wget * | bash"
    - "curl * | sh"
    - "shutdown"
    - "reboot"
    - "init 0"
    - "systemctl"
  command_whitelist_mode: false       # 如果为true，只允许白名单中的命令
  command_whitelist:                  # 白名单模式下允许的命令前缀
    - "ls"
    - "cat"
    - "grep"
    - "find"
    - "echo"
    - "python"
    - "node"
    - "npm"
    - "pip"
    - "git"
    - "make"
    - "cargo"
    - "go"
  require_approval_for:              # 需要人工确认的操作类型
    - "file_write"
    - "file_edit"
    - "shell_exec"
  auto_approve_read: true            # 读操作自动批准

# ===== 上下文管理配置 =====
context:
  max_tokens: 120000                  # 上下文窗口最大token数
  compression_threshold: 0.70         # 使用量达到70%时触发压缩
  critical_threshold: 0.85            # 达到85%时强制压缩
  preserve_recent_turns: 4            # 始终保留最近的N轮对话（不压缩）
  preserve_system_prompt: true        # 始终保留system prompt
  compression_strategy: "summary"     # summary | truncate | hybrid
  save_full_history: true             # 将完整对话历史保存到文件
  history_file: ".codepilot_history.jsonl"

# ===== 显示配置 =====
ui:
  theme: "monokai"                   # 主题
  show_token_usage: true             # 显示token使用量
  show_cost_estimate: true           # 显示费用估算
  show_tool_calls: true              # 显示工具调用详情
  show_thinking: true                # 显示模型思考过程（如果有）
  spinner_style: "dots"              # 加载动画样式
  max_diff_lines: 50                 # diff显示最大行数
2.2 配置优先级
配置加载优先级从高到低：

命令行参数 --provider, --model, --api-key, --workspace
环境变量 CODEPILOT_PROVIDER, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY
当前目录 .codepilot.yml
用户目录 ~/.config/codepilot/config.yml
程序内置默认值
三、API Provider 适配层详细设计
3.1 DeepSeek Provider (providers/deepseek.py)
29
 DeepSeek API 使用与 OpenAI/Anthropic 兼容的 API 格式。通过修改配置，你可以使用 OpenAI SDK 来访问 DeepSeek API。 
30
 实际操作中，开发者一般创建API key，设置 `https://api.deepseek.com` 作为 OpenAI 兼容的 base URL，然后调用 `/chat/completions` 端点。
Python

"""
核心实现要点:
1. 使用 openai Python SDK，将 base_url 设置为 "https://api.deepseek.com"
2. 模型名使用 "deepseek-chat" 或 "deepseek-v4-pro" / "deepseek-v4-flash"
3. tool_call 使用 OpenAI 格式的 tools 参数和 function calling
4. 流式响应使用 stream=True 并逐chunk处理
5. 支持 thinking mode: extra_body={"thinking": {"type": "enabled"}}

关键差异注意:
- DeepSeek 推荐 temperature=1.0, top_p=1.0（与OpenAI不同）
- thinking模式下 temperature/top_p/presence_penalty/frequency_penalty 不生效
- tool_call的arguments是JSON字符串，需要自行parse
- 需要处理 reasoning_content 字段（思考链内容）
"""

# DeepSeek tool call 格式（同OpenAI格式）：
# 请求中的 tools 定义:
tools = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file at the specified path",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The file path to read, relative to workspace root"
                    }
                },
                "required": ["path"]
            }
        }
    }
]

# 响应中的 tool_calls:
# response.choices[0].message.tool_calls -> 列表
# 每个 tool_call: { id, type:"function", function: {name, arguments(JSON字符串)} }
# 
# 将结果返回给模型:
# {"role": "tool", "tool_call_id": "xxx", "content": "文件内容..."}
3.2 Anthropic Provider (providers/anthropic.py)
47
 Anthropic 称此功能为"tool use"，响应格式与OpenAI截然不同。Claude 使用 content-block 架构，tool calls 和文本作为独立的 blocks 出现在 assistant 的响应中。每个 tool_use block 有 id、tool 名称和一个 input 对象（已解析的参数对象，不是JSON字符串）。 
44
 如果Claude决定使用工具，API响应不会包含最终答案，而是会有一个值为 `tool_use` 的 `stop_reason`，content block 会指定工具调用详情。
Python

"""
核心实现要点:
1. 使用 anthropic Python SDK
2. 工具定义使用 Anthropic 原生格式（name, description, input_schema），不是OpenAI的function wrapper
3. 响应的 content 是一个 list，包含 type="text" 和 type="tool_use" 两种block
4. stop_reason == "tool_use" 时表示模型想调用工具
5. 返回工具结果时：发送 role="user" 消息，content 中包含 type="tool_result" 的block

关键格式:

工具定义（Anthropic原生格式，注意没有"type":"function"的wrapper）:
"""
tools = [
    {
        "name": "read_file",
        "description": "Read the contents of a file at the specified path",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read, relative to workspace root"
                }
            },
            "required": ["path"]
        }
    }
]

# 响应中的 tool_use（在 response.content 列表中）:
# {"type": "tool_use", "id": "toolu_xxx", "name": "read_file", "input": {"path": "main.py"}}
# 注意: input 已经是 dict，不需要 JSON.parse
# 
# 将结果返回给模型:
# role="user", content=[{"type": "tool_result", "tool_use_id": "toolu_xxx", "content": "文件内容..."}]
3.3 统一抽象接口 (providers/base.py)
Python

"""
设计一个 BaseProvider 抽象基类，统一两种Provider的差异:

class BaseProvider(ABC):
    @abstractmethod
    async def chat(self, messages, tools, stream=True) -> AsyncIterator[AgentEvent]:
        '''发送消息并获取响应，统一返回 AgentEvent 流'''
        pass

AgentEvent 是一个统一的事件类型:
- TextDelta(text: str)          -> 文本片段
- ThinkingDelta(text: str)      -> 思考过程片段
- ToolCall(id, name, arguments) -> 工具调用请求
- Usage(input_tokens, output_tokens) -> token用量
- Done(stop_reason)             -> 结束

两个子类分别将 DeepSeek(OpenAI格式) 和 Anthropic(Messages API格式) 的响应
统一转换为 AgentEvent 流。
"""
四、工具系统详细设计
4.1 所有工具定义
实现以下 7 个核心工具，每个工具都要有完善的参数校验和安全检查：

text

┌──────────────────────────────────────────────────────────┐
│  Tool Name      │ Description                            │
├──────────────────────────────────────────────────────────┤
│  read_file      │ 读取指定文件内容（带行号显示）            │
│  write_file     │ 创建或覆写文件（需确认，显示diff）        │
│  edit_file      │ 搜索替换方式编辑文件局部内容              │
│  list_files     │ 递归列出目录树（可配置深度和过滤）        │
│  shell_exec     │ 在workspace中执行终端命令                │
│  search_code    │ 在代码库中搜索字符串/正则（grep风格）     │
│  get_context    │ 获取当前上下文使用统计信息                │
└──────────────────────────────────────────────────────────┘
4.2 每个工具必须：
经过 sandbox.py 路径安全校验后才能执行
危险操作（写入、删除、执行命令）需要经过 approval.py 用户确认
返回结构化结果字符串给LLM
对输出大小做截断处理（防止单个文件太大撑爆上下文）
记录详细日志
4.3 shell_exec 工具特殊设计
Python

"""
shell_exec 工具是最危险的，需要特别注意:

1. 命令在 workspace_root 目录下使用 subprocess 执行
2. 设置 cwd=workspace_root
3. 设置 timeout（默认30秒，可配置）
4. 捕获 stdout 和 stderr
5. 输出截断到最多 max_output_lines 行（默认200行）
6. 执行前经过命令黑名单过滤（command_filter.py）
7. 禁止执行交互式命令（vim, nano, less等）
8. 返回格式:
   - exit_code: int
   - stdout: str (截断后)
   - stderr: str (截断后)
   - truncated: bool
   - duration_ms: int
"""
五、安全系统详细设计
20
 真实案例警示：曾有Claude Code用户运行清理任务时执行了 `rm -rf ~/`，删除了整个主目录包括不可替代的家庭照片。 
21
 文件作用域权限必须将读写操作限制在批准的目录内，对机密文件、构建脚本和生产清单采用默认拒绝规则。
5.1 目录沙箱 (security/sandbox.py)
Python

"""
实现路径安全校验器:

class Sandbox:
    def __init__(self, workspace_root: str, allowed_dirs: list, blocked_paths: list):
        self.workspace_root = os.path.realpath(workspace_root)
        # ...

    def validate_path(self, path: str, operation: str = "read") -> tuple[bool, str]:
        '''
        校验路径是否安全
        
        核心逻辑:
        1. 将输入路径 resolve 为绝对路径 (os.path.realpath)
        2. 检查是否包含路径遍历攻击 (../, symbolic link 解析后的真实路径)
        3. 检查是否在 blocked_paths 中（精确匹配 + 前缀匹配）
        4. 检查是否在 workspace_root 内或 allowed_dirs 内
        5. 对于 write 操作，额外检查是否尝试写入 .git/, .codepilot.yml 等敏感文件
        6. 防止符号链接逃逸: 先 resolve 符号链接再检查路径

        返回: (is_safe: bool, reason: str)
        '''
        pass

    def validate_command(self, command: str) -> tuple[bool, str]:
        '''
        校验终端命令是否安全
        
        逻辑:
        1. 拆解命令（考虑 pipe |, &&, ||, ; 等链式命令，每段都要检查）
        2. 检查黑名单关键字
        3. 如果是白名单模式，检查命令前缀是否在白名单中
        4. 检查是否有路径参数越界（如 rm 的目标路径）
        5. 检查是否试图修改文件权限到危险级别
        6. 禁止 sudo / su
        7. 禁止交互式命令

        返回: (is_safe: bool, reason: str)
        '''
        pass
5.2 人工审批 (security/approval.py)
Python

"""
对于 require_approval_for 中列出的操作类型，在执行前显示详细信息请用户确认:

def request_approval(operation: str, details: dict) -> bool:
    '''
    显示操作详情面板，让用户选择 [Y]es / [N]o / [A]lways for this session

    对于 file_write/file_edit: 显示彩色 diff 预览
    对于 shell_exec: 显示完整命令，高亮危险部分
    
    用户可选:
    - y: 本次批准
    - n: 拒绝
    - a: 本次会话中自动批准同类操作
    - !: 进入"YOLO模式"，后续所有操作自动批准（显示警告）
    '''
六、上下文管理与压缩详细设计
11
 这就是Chroma研究团队所说的"上下文腐烂"(context rot)——随着不相关历史记录挤占上下文窗口，响应质量逐渐退化。修复需要对智能体记住什么、卸载什么、何时总结进行原则性处理。上下文压缩是在保留完成任务所需一切的同时减少智能体工作内存信息量的做法。 
15
 采用分层压缩（40%/60%/95% 阈值）、基于LLM的智能摘要、文件级细粒度压缩和语义搜索集成的内存管理方案。
6.1 Token 计数器 (context/token_counter.py)
Python

"""
实现token估算:
- 如果安装了 tiktoken，使用 cl100k_base 编码器精确计数
- 否则使用字符数 / 3.5 的粗略估算
- 对每条消息统计token数并缓存
- 提供 count_messages(messages) -> int 方法
"""
6.2 上下文管理器 (context/manager.py)
Python

"""
class ContextManager:
    管理完整的消息历史，在每次LLM调用前检查是否需要压缩

    核心数据结构:
    - system_prompt: str          # 系统提示词（始终保留）
    - messages: list[Message]     # 完整消息历史
    - compressed_summary: str     # 已压缩部分的摘要
    - total_tokens: int           # 当前总token数
    
    核心方法:
    - add_message(role, content)  # 添加消息
    - get_context() -> list       # 获取当前上下文（可能触发压缩）
    - force_compress()            # 手动触发压缩
    - get_stats() -> dict         # 获取统计信息

    上下文窗口组成:
    ┌─────────────────────────────────────────────┐
    │  [System Prompt]        (~500 tokens, 固定)  │
    │  [Compressed Summary]   (动态摘要)           │
    │  [Recent Messages]      (最近N轮完整对话)     │
    │  [Current Turn]         (当前轮次)            │
    └─────────────────────────────────────────────┘
"""
6.3 上下文压缩器 (context/compressor.py)
11
 最近10%的上下文窗口始终逐字保留，因为它包含智能体的活跃工作记忆——当前工具调用、最近的用户消息和紧前推理。将这些摘要掉会破坏智能体继续执行中间任务的能力。 
15
 使用智能体自身的LLM生成压缩对话段的信息密集摘要。摘要提示明确指示保留：变量/函数/类名、文件路径、错误信息及解决方案、设计决策及理由、任务进度、工具调用结果和约束条件。结构化摘要格式包括：CONTEXT（正在处理什么）、ACTIONS（使用的工具、编写的代码）、OUTCOMES（结果、修复的错误）、NEXT STEPS（剩余任务）和IMPORTANT REFERENCES（需要记住的关键实体）。
Python

"""
class ContextCompressor:
    
    压缩策略 "summary"（推荐）:
    
    当 total_tokens / max_tokens >= compression_threshold 时触发:
    
    1. 分区:
       - 不可压缩区: system_prompt + 最近 preserve_recent_turns 轮对话
       - 可压缩区: 其余的历史消息
    
    2. 构建压缩提示:
       发送可压缩区的消息给LLM（可以用同一个模型或更便宜的模型），
       使用如下摘要提示:
       
       '''
       Please compress the following conversation history into a structured summary.
       You MUST preserve:
       - All file paths mentioned
       - All function/class/variable names
       - All error messages and their resolutions
       - Design decisions and their rationale
       - Current task progress and status
       - Key tool call results
       
       Format your summary as:
       ## CONTEXT
       [What was being worked on]
       
       ## KEY ACTIONS TAKEN
       [Tools used, code written, files modified]
       
       ## OUTCOMES
       [Results achieved, errors fixed, tests passed/failed]
       
       ## CURRENT STATE
       [Where we are now, what's pending]
       
       ## IMPORTANT REFERENCES
       [File paths, function names, config values to remember]
       '''
    
    3. 用摘要替换可压缩区:
       将压缩后的摘要作为一条 system/assistant 消息插入到 system_prompt 之后
    
    4. 将原始完整历史写入 history_file 以备恢复
    
    5. 在终端显示压缩通知:
       "📦 Context compressed: 85,234 → 32,100 tokens (62% reduction)"
    
    压缩策略 "truncate"（简单模式）:
    - 直接丢弃最早的消息，保留最近的对话
    
    压缩策略 "hybrid":
    - 对工具输出（特别是大文件读取结果）用截断
    - 对对话部分用摘要
    - 优先压缩：文件内容 > 命令输出 > 对话历史
"""
七、核心 Agent 循环 (agent/loop.py)
Python

"""
实现标准的 Agentic Tool-Use 循环:

async def agent_loop(user_input: str):
    1. 将用户输入加入 context_manager
    2. while True:
        a. context = context_manager.get_context()  # 可能触发压缩
        b. 调用 provider.chat(context, tools, stream=True)
        c. 流式输出文本部分到终端
        d. 如果响应包含 tool_calls:
            - 对每个 tool_call:
                i.   在UI中显示工具调用信息（工具名、参数）
                ii.  安全校验（sandbox + command_filter）
                iii. 如需确认，调用 approval.request_approval()
                iv.  执行工具，获取结果
                v.   在UI中显示工具执行结果（截断显示）
                vi.  将 tool_call 和 tool_result 加入 context_manager
            - continue (继续循环，让模型处理工具结果)
        e. 如果响应是纯文本（stop_reason 不是 tool_use）:
            - 将 assistant 消息加入 context_manager
            - break (回到用户输入)
    3. 显示token使用统计
    4. 等待下一个用户输入

注意事项:
- 单轮中最多允许 25 次工具调用（防止无限循环）
- 如果工具执行失败，将错误信息作为 tool_result 返回给模型
- 每次工具调用后检查上下文是否需要压缩
"""
八、终端UI显示设计（使用 rich 库）
UI显示是验证项目是否成功的关键指标，必须做好

8.1 启动界面
text

╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     ██████╗ ██████╗ ██████╗ ███████╗██████╗ ██╗██╗      ║
║    ██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗██║██║      ║
║    ██║     ██║   ██║██║  ██║█████╗  ██████╔╝██║██║      ║
║    ██║     ██║   ██║██║  ██║██╔══╝  ██╔═══╝ ██║██║      ║
║    ╚██████╗╚██████╔╝██████╔╝███████╗██║     ██║███████╗ ║
║     ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚══════╝ ║
║                                                              ║
║    🤖 AI Coding Agent CLI v0.1.0                             ║
║                                                              ║
║    Provider:  DeepSeek (deepseek-chat)                       ║
║    Workspace: /home/user/my-project                          ║
║    Security:  Sandbox ON | Approval ON                       ║
║    Context:   120K tokens max | Auto-compress at 70%         ║
║                                                              ║
║    Commands:  /help  /config  /clear  /compact  /stats       ║
║               /undo  /history  /quit                         ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
8.2 交互显示
每个交互轮次的显示要素：

text

┌─ 用户输入 ──────────────────────────────────────────────────
│ You › 请帮我创建一个 Express 服务器，带有 /health 端点
└─────────────────────────────────────────────────────────────

┌─ 工具调用 ──────────────────────────────────────────────────
│ 🔧 list_files(path=".", max_depth=2)
│ 📂 Result: 12 files found
│    ├── package.json
│    ├── src/
│    │   ├── index.ts
│    │   └── routes/
│    └── ...
└─────────────────────────────────────────────────────────────

┌─ 工具调用 ──────────────────────────────────────────────────
│ 🔧 write_file(path="server.js")
│ ⚠️  Requires approval:
│ ┌─ Preview ──────────────────────────────────────────────
│ │ + const express = require('express');
│ │ + const app = express();
│ │ +
│ │ + app.get('/health', (req, res) => {
│ │ +   res.json({ status: 'ok', timestamp: Date.now() });
│ │ + });
│ │ +
│ │ + app.listen(3000, () => {
│ │ +   console.log('Server running on port 3000');
│ │ + });
│ └────────────────────────────────────────────────────────
│ Approve? [Y]es / [N]o / [A]lways > y
│ ✅ File written: server.js (9 lines)
└─────────────────────────────────────────────────────────────

┌─ 工具调用 ──────────────────────────────────────────────────
│ 🔧 shell_exec(command="node server.js &")
│ ⚠️  Requires approval:
│ 💻 Command: node server.js &
│ Approve? [Y]es / [N]o / [A]lways > y
│ ✅ Exit code: 0 | Duration: 120ms
│ 📤 stdout: Server running on port 3000
└─────────────────────────────────────────────────────────────

┌─ Assistant ─────────────────────────────────────────────────
│ 我已经为你创建了一个基本的 Express 服务器 (`server.js`)，
│ 包含一个 `/health` 端点。服务器已在 3000 端口启动。
│ 
│ 你可以通过 `curl http://localhost:3000/health` 来测试。
└─────────────────────────────────────────────────────────────

── Token Usage ───────────────────────────────────────────────
   Input: 1,234 | Output: 567 | Total: 1,801
   Context: 1,801 / 120,000 (1.5%) | Est. Cost: $0.002
──────────────────────────────────────────────────────────────
8.3 安全拒绝显示
text

┌─ 🛑 SECURITY BLOCK ────────────────────────────────────────
│ Operation: shell_exec
│ Command:   rm -rf /important/data
│ Reason:    Path "/important/data" is outside workspace
│            Workspace: /home/user/my-project
│            Command matches blacklist pattern: "rm -rf /"
│ 
│ The agent's request has been blocked. The agent will be
│ informed of this restriction.
└─────────────────────────────────────────────────────────────
8.4 上下文压缩通知
text

┌─ 📦 Context Compression ───────────────────────────────────
│ Trigger:    Context usage reached 72% (86,400 / 120,000)
│ Strategy:   LLM Summary
│ Compressed: 67 messages → summary (3,200 tokens)
│ Before:     86,400 tokens
│ After:      38,700 tokens (55% reduction)
│ Preserved:  Last 4 turns (uncompressed)
│ Full history saved to: .codepilot_history.jsonl
└─────────────────────────────────────────────────────────────
8.5 Slash 命令
Python

"""
实现以下 slash 命令:

/help          - 显示帮助信息
/config        - 显示当前配置
/stats         - 显示详细统计（token使用、工具调用次数、费用等）
/clear         - 清空对话历史（重新开始）
/compact       - 手动触发上下文压缩
/history       - 显示对话历史概要
/model <name>  - 切换模型
/provider <p>  - 切换provider（deepseek/anthropic）
/approve       - 切换自动批准模式
/undo          - 撤销最近的文件操作（如果可能）
/quit 或 /exit - 退出
Ctrl+C         - 中断当前操作
Ctrl+D         - 退出
"""
九、System Prompt
Python

SYSTEM_PROMPT = """You are CodePilot, an expert AI coding assistant operating in a terminal environment.

## Your Capabilities
You have access to the following tools to help users with coding tasks:
- read_file: Read file contents
- write_file: Create or overwrite files
- edit_file: Make targeted edits using search/replace
- list_files: List directory contents
- shell_exec: Execute terminal commands
- search_code: Search for patterns in code
- get_context: Check current context window usage

## Guidelines
1. ALWAYS use tools to inspect the codebase before making changes. Read relevant files first.
2. When editing files, prefer edit_file (surgical changes) over write_file (full replacement) for existing files.
3. After making changes, verify them by reading the file back or running tests.
4. When running commands, explain what the command does before executing.
5. If a tool call fails or is blocked by security, acknowledge it and try an alternative approach.
6. Keep your responses concise and focused on the task.
7. When creating new files, always show the full content in write_file.
8. For complex tasks, break them down into steps and execute them one by one.

## Constraints  
- You can ONLY access files within the designated workspace directory.
- Some operations require user approval before execution.
- Destructive commands (rm -rf, etc.) may be blocked by security policy.
- If you encounter a security restriction, do NOT try to bypass it.

## Response Style
- Be direct and actionable
- Use markdown formatting in explanations
- Show code changes clearly
- Provide brief explanations of what you're doing and why
"""
十、CLI 入口和命令行参数 (main.py)
Python

"""
使用 argparse 或 click 实现:

codepilot                           # 交互模式（默认）
codepilot "fix the bug in main.py"  # 单次执行模式
codepilot --provider anthropic      # 指定provider
codepilot --model deepseek-v4-pro   # 指定模型
codepilot --workspace ./my-project  # 指定工作目录
codepilot --api-key sk-xxx          # 直接传入API key
codepilot --no-approve              # 禁用审批（YOLO模式）
codepilot --config path/to/config   # 指定配置文件
codepilot --verbose                 # 详细日志模式
codepilot --version                 # 显示版本
"""
十一、依赖 (requirements.txt)
text

openai>=1.40.0          # DeepSeek API（OpenAI兼容）
anthropic>=0.40.0       # Anthropic Claude API
rich>=13.7.0            # 终端UI（面板、表格、Markdown、进度条、语法高亮）
prompt_toolkit>=3.0.0   # 高级输入（历史、自动补全、多行编辑）
pyyaml>=6.0             # YAML配置解析
tiktoken>=0.7.0         # Token计数（可选，安装失败也能工作）
十二、开发顺序和验收标准
请按以下顺序开发，每完成一步要确保能运行验证：

Phase 1: 基础骨架
创建项目结构和所有空文件
实现配置加载（config.py）
实现启动 banner 和基本 REPL 循环（main.py + ui/banner.py）
验收: 运行 python main.py 能看到banner，能输入文字，输入 /quit 退出
Phase 2: Provider 适配
实现 providers/base.py 抽象接口
实现 providers/deepseek.py（使用 openai SDK）
实现 providers/anthropic.py（使用 anthropic SDK）
验收: 能发送简单消息并得到流式响应显示在终端
Phase 3: 工具系统
实现 tools/registry.py 工具注册表
实现所有 7 个工具
将工具定义转换为两种Provider各自的格式
验收: 发送 "list all files in current directory" 能正确触发 list_files 工具并显示结果
Phase 4: Agent 循环
实现 agent/loop.py 核心循环
实现工具调用→执行→结果回传的完整流程
验收: 能完成多步任务，如 "read main.py and add error handling"
Phase 5: 安全系统
实现 security/sandbox.py
实现 security/command_filter.py
实现 security/approval.py
验收: 尝试读取 /etc/passwd 被拒绝；写文件时显示 diff 并请求确认
Phase 6: 上下文管理
实现 context/token_counter.py
实现 context/manager.py
实现 context/compressor.py
验收: /stats 命令显示 token 统计；人工触发 /compact 能压缩上下文
Phase 7: 完善UI
完善所有 Rich 面板显示
实现 diff 着色显示
实现所有 slash 命令
错误处理和边界情况
验收: 完整的一轮交互（用户提问→工具调用→确认→结果→回答）所有UI元素正常显示
十三、关键实现注意事项
所有文件I/O使用 async：使用 aiofiles 或在线程池中运行同步操作
流式显示：LLM 响应必须流式显示（一个字一个字出现），不能等全部生成完才显示
错误恢复：网络错误、API错误、工具执行错误都要优雅处理，不能crash
Ctrl+C 处理：中断当前LLM调用，回到输入提示符，不退出程序
编码处理：文件读写统一使用 UTF-8，遇到二进制文件要跳过
路径处理：所有路径统一用 os.path.realpath() 解析后再比较
大文件保护：读取文件超过 100KB 时自动截断，并告知LLM文件被截断
并发安全：如果用 async，注意 context_manager 的线程安全
现在，请开始按照 Phase 1 到 Phase 7 的顺序逐步实现整个项目。每完成一个 Phase，简要说明完成了什么以及如何验证。确保代码可以直接运行，不要留占位符或 TODO。