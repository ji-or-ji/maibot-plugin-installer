# 插件安装审批器（maibot-plugin-installer）

把「造好的麦麦插件」经**管理员批准**后自动安装并启用，在发起任务的聊天流里请求审批。是 [麦麦×dsh 桥](https://github.com/ji-or-ji/maibot-dsh-bridge) 的配套下游：桥造出插件，它负责走审批与上线。

## 功能

- **对外接口 `submit`**（跨插件 API）：其他插件交来一个待安装插件目录 + 发起流，本插件在该流里发出审批请求。
- **命令**（仅管理员）：
  - `/pi_approve <id>` —— 批准：复制到 `plugins/<id>/`、启用、由宿主自动加载。
  - `/pi_deny <id>` —— 忽略：撤销待审，**源目录原样保留**（不删）。
  - `/pi_pending` —— 列出当前待审插件。

## 依赖

- **必需**：`ji-or-ji.maibot-dsh-bridge`（桥）。本插件是桥的下游，单独存在没有意义——没有桥给它提交插件。
  - 因此 **安装顺序：先装桥，再装本插件**（manifest 已声明对该桥的插件依赖）。

## 安装

把本仓库目录放进麦麦的 `plugins/` 下（目录名建议 `maibot-plugin-installer`），重载麦麦。**先确保桥已装好。**

## 配置流程

1. 复制 `config.toml.example` 为 `config.toml`。
2. 在 `[permission].allowed_users` 填入**有权审批的管理员**用户 ID（逗号分隔，如 `qq:123456`）。**留空表示拒绝所有人**（fail-closed）；想开放必须显式列出。
3. `[plugin].enabled` 设为 `true`，重载麦麦。

其余（`plugins_root`、`auto_reload`、`api_endpoint`）都有合理默认，一般无需改。

## 工作流程

```
dsh 桥造出插件 → 桥调用本插件 submit → 本插件在发起流发审批请求
→ 管理员 /pi_approve <id> → 复制进 plugins/<id>、启用、宿主加载上线
```

## 安全边界

- **只有白名单里的管理员**能审批；留空即全员拒绝。
- 安装目标被强制锁定在 `plugins/` 根目录内，越界拒绝。
- `/pi_deny` 不删除源目录，可回滚。
- 本插件**只写被批准的那一个插件目录**，不碰别的。

## 许可

MIT
