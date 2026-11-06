# ONES 新建任务接口调查

调查日期：2026-11-06。本文记录当前 ONES Project 网页使用的内部接口，服务升级后可能变化。基础路径为 `/project/api/project/team/{team_uuid}`，沿用本项目已有的登录和鉴权配置。

## 证据与验证范围

- 在网页任务创建表单提交时拦截了请求，确认请求地址和 JSON；请求在发送前被中止，没有创建测试任务。
- 项目字段配置和团队字段定义接口已进行只读请求验证；接入后的 Gateway 也已用当前项目进行只读字段查询，并确认生成的字段与网页请求一致。
- 创建成功的服务端响应尚未实测。响应处理根据前端代码中对 `tasks` 和 `bad_tasks` 的使用实现；实际部署若返回不同结构，会报告结果不确定及生成的任务 UUID，应先查询 ONES 再决定是否重试。

## 字段发现

调用 `POST /project/{project_uuid}/stamps/data?t=field_config,issue_type_config`，请求体为 `{"field_config":0,"issue_type_config":0}`；调用 `POST /stamps/data?t=task_field,task_field_option`，请求体为 `{"task_field":0,"task_field_option":0}`。前者返回项目及任务类型对应的字段配置，后者返回字段名称和选项。

通过 MCP 创建前，先使用 `list_project_issue_types` 选出项目启用且 `component_type` 为 `task` 的任务类型，再调用 `get_task_create_fields(project_id, issue_type_id)` 获取字段、必填标记、默认值和选项。字段由项目和任务类型决定，不要将某个项目的自定义字段 ID 当成全局常量。

## 新建任务

`POST /tasks/add3`，网页请求示例（值均为示意）：

```json
{
  "tasks": [{
    "uuid": "<当前用户UUID><8位随机字符>",
    "assign": "<负责人UUID>",
    "summary": "任务标题",
    "parent_uuid": "",
    "field_values": [
      {"field_uuid": "field004", "type": 8, "value": "<负责人UUID>"},
      {"field_uuid": "field012", "type": 1, "value": "<优先级选项UUID>"},
      {"field_uuid": "field011", "type": 7, "value": "<迭代UUID>"},
      {"field_uuid": "<开始日期字段UUID>", "type": 5, "value": 1793894400, "date_value": "2026-11-06"},
      {"field_uuid": "field013", "type": 5, "value": 1793894400, "date_value": "2026-11-06"},
      {"field_uuid": "field001", "type": 2, "value": "任务标题"},
      {"field_uuid": "field016", "type": 20, "value": null}
    ],
    "issue_type_uuid": "<任务类型UUID>",
    "project_uuid": "<项目UUID>",
    "watchers": ["<当前用户UUID>"]
  }]
}
```

本项目 `create_task` MCP 工具根据字段发现结果组装请求，返回 `task_id` 供 `add_manhour` 使用。`field_values` 使用 `field_id`、`value` 和可选的 `date_value`，而非直接填写网页请求中的 `field_uuid` 和 `type`。日期需要目标时区当天零点的 Unix 秒数及 `YYYY-MM-DD`；单选、迭代和成员字段使用精确 UUID，文本字段使用字符串。当前支持类型 1、2、5、7 和 8；系统管理字段、生成字段和任务描述由服务端/工具处理。负责人默认当前用户，当前用户会成为关注者。工具不自动重试创建请求。

项目可能要求额外字段或有不同权限；字段类型尚不受支持时需要扩展实现。新建请求的超时、网络故障或无法识别的响应可能发生在服务器已创建任务之后，错误中包含生成的任务 UUID，检查 ONES 后再决定是否重试。
