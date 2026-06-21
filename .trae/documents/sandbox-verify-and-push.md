# 计划：沙箱验证 + CLI 实测 + 推送 GitHub

## 总结

1. 沙箱功能已完整实现（路径校验 + 命令过滤 + 审批系统），需验证实际运行效果
2. 用真实 API（讯飞星辰 + DeepSeek）测试 CLI 基本工作能力
3. 清理遗留代码后推送到 GitHub

## 现状分析

### 沙箱功能
- **已完整实现**：`src/codepilot/security/sandbox.py`（路径校验）、`command_filter.py`（命令过滤）、`approval.py`（审批系统）
- 路径校验：阻止路径遍历、符号链接逃逸、敏感文件写入（.git/、__pycache__/、.codepilot.yml）
- 命令校验：黑名单（rm -rf /、mkfs 等）、提权（sudo/su）、交互式（vim/nano/less）、白名单模式
- 审批系统：支持 y/n/a/! 四种选择，YOLO 模式（--no-approve）
- **已集成到全部 7 个工具**：文件工具走 validate_path，shell_exec 走 validate_command
- **440 个测试通过**，含 test_security.py 的完整覆盖

### CLI 配置
- 入口：`codepilot` 命令或 `python -m codepilot`
- Provider 默认配置已指向讯飞星辰端点：
  - DeepSeek: `base_url = https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`，model `astron-code-latest`
  - Anthropic: `base_url = https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic`，model `astron-code-latest`
- 配置优先级：CLI 参数 > 环境变量 > .codepilot.yml > 内置默认

### Git 状态
- 已初始化 git 仓库（master 分支，1 个 commit）
- 无远程仓库配置
- 根目录存在遗留的 `codepilot/` 目录（src layout 重构前的旧代码，需清理）

### 安全注意
- 用户提供了 API Key 和 GitHub PAT，**绝不能提交到 git 仓库**
- `.codepilot.yml` 必须加入 `.gitignore`

## 计划步骤

### Step 1: 清理遗留代码 + 更新 .gitignore

**文件**: `.gitignore`
- 新增 `.codepilot.yml`（含 API Key，不能提交）
- 新增 `.codepilot/`（运行时缓存目录）
- 新增 `.codepilot_cache/`（旧缓存目录）
- 新增 `pytest_out.txt`、`pytest_cov.txt`（测试输出文件）

**删除**: 根目录 `codepilot/` 目录（遗留旧代码，与 `src/codepilot/` 重复，pyproject.toml 声明 `packages = ["src/codepilot"]`）

### Step 2: 创建 .codepilot.yml 配置文件（不提交）

**文件**: `.codepilot.yml`（本地使用，已在 .gitignore 中排除）

配置两个 provider：
- DeepSeek provider 使用讯飞星辰端点（默认配置），API Key 通过环境变量 `CODEPILOT_API_KEY` 传入
- 如果需要切换到 DeepSeek 官方 API，通过 CLI 参数 `--api-key` 和 `--provider deepseek` 临时指定

实际测试时用环境变量方式传入 API Key，避免写入文件：
```powershell
$env:CODEPILOT_API_KEY = "讯飞星辰的key"
codepilot
```

### Step 3: 验证沙箱功能

运行现有安全测试确认沙箱功能正常：
```powershell
pytest tests/unit/test_security.py -v
```

### Step 4: 用真实 API 测试 CLI 基本功能

使用讯飞星辰 API（默认配置）测试以下基本操作：

1. **启动 CLI**：`codepilot --version` 确认安装正常
2. **单次模式测试**：`echo "列出当前目录的文件" | codepilot --no-approve` 或进入交互模式
3. **交互模式测试**：启动 `codepilot`，测试以下基本任务：
   - 读取文件：让 AI 读取 README.md
   - 列出文件：让 AI 列出项目结构
   - 搜索代码：让 AI 搜索某个函数定义
   - 写入文件：让 AI 创建一个简单的测试文件
   - Shell 执行：让 AI 运行 `python --version`
4. **沙箱验证**：尝试让 AI 执行危险命令（如 `rm -rf /`），确认被拦截

如果讯飞星辰 API 不可用，切换到 DeepSeek 官方 API：
```powershell
$env:CODEPILOT_API_KEY = "sk-b04dcf625d2d4338aa73e3a938b42bad"
codepilot --provider deepseek --model deepseek-chat
```

### Step 5: 推送到 GitHub

1. 添加远程仓库：`git remote add origin https://github.com/FCFColin/CodePilot.git`（仓库名需确认）
2. 添加所有文件（排除 .gitignore 中的文件）
3. 提交所有变更
4. 推送到 GitHub（使用 PAT 认证）

## 假设与决策

1. **讯飞星辰 API 为默认 provider**：项目 config.py 已将 DeepSeek provider 的 base_url 默认指向讯飞星辰端点，model 为 `astron-code-latest`，无需修改代码
2. **API Key 通过环境变量传入**：避免写入任何文件，最安全
3. **遗留 `codepilot/` 目录删除**：与 `src/codepilot/` 完全重复，pyproject.toml 只引用 `src/codepilot`
4. **GitHub 仓库名**：`FCFColin/CodePilot`（需先在 GitHub 上创建空仓库）
5. **CLI 测试为手动交互式**：编码智能体 CLI 需要真实 LLM API 调用，自动化测试有限

## 验证步骤

1. `pytest tests/unit/test_security.py -v` — 沙箱测试全部通过
2. `codepilot --version` — 输出版本号
3. 交互式测试 CLI 基本功能（读文件、写文件、搜索、shell 执行）
4. 沙箱拦截验证（危险命令被拒绝）
5. `git status` — 确认 .codepilot.yml 不在暂存区
6. `git push` 成功
