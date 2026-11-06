# ONES 工时接口调查

调查及验证日期：2026-11-05 至 2026-11-06。目标为当前部署的 ONES Project 网页内部接口，属于非公开实现，升级后可能变化。团队 UUID 在本文中写作 `{team_uuid}`，基础路径为 `/project/api/project/team/{team_uuid}`。

## 证据与范围

- 已通过网页网络请求观察：工时报表、工作项工时汇总、成员当日可登记工时、工作项搜索、工作日历。
- 已通过浏览器实际添加一条测试工时，使用前端代码中的 GraphQL 结构执行明细查询、修改并回查；修改前后返回的数值与描述一致。
- 对同一测试记录调用删除接口时，服务端返回 HTTP 403 和 `PermissionDenied.ManageTaskRecordManhours`，删除未成功。该次验证确认请求到达权限检查，但未验证删除成功响应。
- 简单模式下预估/剩余工时接口来自前端代码静态分析，尚未发送写请求。
- 不保存真实凭据。网页请求携带 `Authorization`、Cookie 和 JSON Content-Type；本仓库现有 ONES 客户端使用 `Authorization: Bearer <token>`。实际调用使用已有的授权会话与权限，不要将凭据写入日志。

## 添加实际工时

`POST /project/api/project/team/{team_uuid}/items/graphql?t=AddManhour`

以下是网页在简单工时模式中生成的请求，示例值已替换：

```json
{
  "query": "mutation AddManhour { addManhour (mode: $mode owner: $owner task: $task type: $type start_time: $start_time hours: $hours description: $description) { key } }",
  "variables": {
    "mode": "simple",
    "owner": "<成员UUID>",
    "task": "<工作项UUID>",
    "type": "recorded",
    "start_time": 1793876400,
    "hours": 100000,
    "description": "工时描述"
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `mode` | 团队工时模式；本次捕获为 `simple`，前端另有 `detailed` 模式，未验证。 |
| `owner` | 登记成员 UUID。 |
| `task` | 工作项 UUID，不是显示的工作项编号。 |
| `type` | 实际工时为 `recorded`；前端还有 `estimated` 类型。 |
| `start_time` | Unix 时间戳，单位秒。选择本地时间后需按对应时区换算。 |
| `hours` | 定点整数；本次 1 小时捕获为 `100000`。前端精度系数为 `100000`。 |
| `description` | 工时描述。 |

选择“手动填写”剩余工时时，网页在 `addManhour` 参数和 `variables` 中额外添加 `remaining_hours`。例如界面填写 2 小时，捕获值为 `200000`；选择“自动计算”时不包含该字段。自动计算模式下实际成功响应的形状为 `{"data":{"addManhour":{"key":"manhour-<记录UUID>"}}}`；手动模式仅截获请求，尚未验证写入。

## 修改与删除记录

以下请求由前端 `733.9178f7c8.js` 的模块 `48IU`、`iqX+` 和登记弹窗代码推导，再通过登录会话直接调用；并非从修改或删除界面抓包得到。它们使用同一 GraphQL 端点。

修改：`POST .../items/graphql?t=UpdateManhour`。下面的参数组合实际返回 HTTP 200 和 `data.updateManhour.key`；之后明细查询确认工时从 `1000` 变为 `2000`，描述也已更新。

```graphql
mutation UpdateManhour {
  updateManhour (mode: $mode owner: $owner task: $task type: $type
    start_time: $start_time hours: $hours description: $description key: $key) { key }
}
```

`variables` 实测包含 `mode`、`owner`、`task`、`type`、`start_time`、`hours`、`description` 和原记录 `key`，与上述参数一一对应。手动填写剩余工时时前端还可能附带 `remaining_hours`，该分支尚未实际验证；哪些字段可省略也未验证。

删除：`POST .../items/graphql?t=DeleteManhour`，使用 `variables` 中的完整记录 `key` 和 `mode`：

```graphql
mutation DeleteManhour {
  deleteManhour (key: $key mode: $mode) { key }
}
```

实际响应为 HTTP 403：

```json
{
  "code": 403,
  "errcode": "PermissionDenied.ManageTaskRecordManhours",
  "graphql_path": ["deleteManhour", "DeleteManhour"],
  "permission": "ManageTaskRecordManhours",
  "type": "PermissionDenied"
}
```

删除后再次查询，记录仍存在；当前账号能添加和修改该记录，但本次没有删除权限。权限授予或由有权限的人员操作后，仍需验证成功响应和记录消失。不要把记录的裸 UUID 与 `key` 混用，应使用查询返回的完整 `key`。

本次待清理的测试记录为工作项 `#7985`、`manhour-XZRZKrTg`，描述为“临时接口验证 20261106-ones-agent 已修改”，实际工时为 0.02 小时。删除失败后，该工作项的 `totalManhour` 为 `2000`；创建前为 `0`，预估和剩余工时均为 `null`。

## 工时明细查询

前端报表的 `UserManhoursTimeSeries` 查询可通过 `POST .../items/graphql?t=UserManhoursTimeSeries` 执行。以下按成员、日期和实际工时类型查询；这是使用登录会话直接执行的真实请求，非界面抓包。

```json
{
  "query": "query UserManhoursTimeSeries { buckets(groupBy: $manhoursGroupBy) { key startDate manhours(filter: $manhoursFilter, orderBy: $manhoursOrderBy) { key hours owner { uuid } startTime(unit: ONESDATE) canBrowseTask canUpdateRecordedHour description type task { key uuid name } } } }",
  "variables": {
    "manhoursGroupBy": { "manhours": { "startDate": {} } },
    "manhoursFilter": {
      "owner_in": ["<成员UUID>"],
      "startDate_range": { "unit": "day", "quick": "this_week" },
      "type_in": ["recorded"]
    },
    "manhoursOrderBy": { "createTime": "DESC" }
  }
}
```

返回 `data.buckets`，每个桶包含 `key`、秒级 `startDate` 和 `manhours` 数组。测试记录返回的 `hours` 为 `1000`（修改后为 `2000`），`startTime` 为秒级时间戳，`key` 形如 `manhour-<记录UUID>`；记录还包含成员、工作项、描述和权限标志。桶是按登记日期分组，`startDate` 与记录的具体 `startTime` 不同。

还验证了 `startDate_range: {"unit":"day", "from":"2026-11-01", "to":"2026-11-06"}`：结束日期包含当天；将 `to` 改为 `2026-11-05` 时，11 月 6 日的记录不再返回。其他筛选组合与分页未验证。HTTP 200 仍需检查响应中的 `data` 或错误字段，不能仅凭状态码判断业务成功。

仓库通过 `OnesGateway` 和 MCP 的 `list_manhours`、`add_manhour`、`update_manhour` 暴露已验证的简单模式和实际工时。用户传入小时数，由网关转换为 ONES 定点整数；修改需要提供查询返回的完整 key 和全部字段。暂不暴露删除或手动剩余工时写入。

## 工作项工时汇总

选中工作项后，网页实际请求 `POST .../items/graphql?t=taskManhoursInfo-<工作项UUID>`：

```json
{
  "query": "query FetchTaskManhoursInfo { tasks(filter: $taskFilter) { key estimatedHours totalManhour remainingManhour remainingHourDeviation } }",
  "variables": { "taskFilter": { "uuid_in": ["<工作项UUID>"] } }
}
```

这是工作项汇总；逐条记录见上面的工时明细查询。

## 当前用户工时报表

“我的工作台 → 工时”实际请求 `POST .../items/graphql?t=report-data__workspace_manhour-{team_uuid}`。捕获的查询和变量如下：

```json
{
  "query": "query QUERY_MANHOURS($groupBy: GroupBy, $orderBy: OrderBy, $timeSeries: TimeSeriesArgs, $actualHoursSum: String, $filter: Filter, $columnSource: Source) { buckets(groupBy: $groupBy, orderBy: $orderBy, filter: $filter) { key columnField: aggregateTask(source: $columnSource) { key uuid name number project { uuid } } actualHours(sum: $actualHoursSum) actualHoursSeries(timeSeries: $timeSeries) { times values } } }",
  "variables": {
    "groupBy": { "manhours": { "task": {} } },
    "orderBy": { "aggregateTask": { "createTime": "DESC" } },
    "filter": { "manhours": { "task_notIn": [null], "startTime_range": { "unit": "day", "quick": "this_week" }, "owner_in": ["$currentUser"] }, "actualHours_notIn": [0] },
    "actualHoursSum": "manhours.recordedHour",
    "timeSeries": { "timeField": "manhours.startTime", "valueField": "manhours.recordedHour", "unit": "day", "quick": "this_week" },
    "columnSource": "task"
  }
}
```

`$currentUser` 是变量值中的特殊字符串，不是需要本地替换的 GraphQL 变量名。该查询按工作项分组，返回合计和按日时间序列。

## 其他已观察请求

| 用途 | 方法与路径 | 请求体或参数 |
| --- | --- | --- |
| 成员当日可登记工时 | `POST .../project/{project_uuid}/recordable_manhour` | `{"record_time": <秒级时间戳>, "owner": "<成员UUID>"}`；前端读取响应的 `value` 和 `limit`，再按 `100000` 换算。 |
| 搜索工作项 | `POST .../search?limit=20&q=<URL编码关键词>&types=related_task` | `{"include_subtasks": true, "filter": null}` |
| 工作日历 | `POST .../workdays_calendar` | `{"quick": "this_week"}` |

简单工时模式下，前端还引用 `POST .../task/{task_uuid}/assess_manhour/update` 和 `POST .../task/{task_uuid}/remaining_hour/update`，数据为 `value`；它们通过不同于 JSON 请求的封装发送，实际编码、单位和响应均未验证。
