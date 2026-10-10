# MCP 集成说明

## 1. 使用的 MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp`（麦当劳 MCP Server） |
| 接入地址 | `https://mcp.mcd.cn`（可用 `MCD_MCP_URL` 覆盖，测试时指向本地 mock） |
| 传输方式 | `streamablehttp`（JSON-RPC 2.0，兼容 `application/json` 与 `text/event-stream` 两种响应） |
| 协议版本 | `2025-06-18` |
| 鉴权 | `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 客户端实现 | `src/mcdgit/mcp.py`（纯标准库，无 `mcp` SDK 依赖） |

Token 的获取顺序（找到第一个就用，**不写入任何文件、不回显**）：

1. `--token` 命令行参数
2. 环境变量 `MCD_MCP_TOKEN`
3. 本机已有的 MCP 客户端配置里的 `headers.Authorization`
   （`~/.pi/agent/mcp.json`、`~/.claude.json`、`~/.cursor/mcp.json`）

第 3 条是为了"用户已经把 Token 配在别的客户端里了，不用再粘一遍"。

---

## 2. 使用的 Tool 清单

> 工具名以**服务端实际返回的为准**：是 kebab-case（`query-meals`），
> 不是文档里常写的 snake_case。用错名字会直接拿到 `-32602 unknown tool`。

| # | Tool（服务端实名） | 用途 | 调用位置 | 核心 |
|---|---|---|:---:|:---:|
| 1 | `query-nearby-stores` | `init`/`remote` 选到店自取(beType=1)或得来速(5)的门店 | `services.search_stores` | ✅ |
| 2 | `delivery-query-addresses` | 麦乐送(2)/团餐(6)先取配送地址 | `commands/setup._resolve_delivery_store` | |
| 3 | `delivery-query-stores` | 按地址查可配送门店，拿 `beCode` | `commands/setup._resolve_delivery_store` | |
| 4 | `query-meals` | 拉当前门店**在售**菜单（`mcdgit fetch`） | `services.fetch_menu` | ✅ |
| 5 | `query-meal-detail` | 取一道餐品的完整特调项（`selectedKey`/`unselectedKey`） | `services.load_mods` | ✅ |
| 6 | `calculate-price` | `mcdgit diff` 的券后价、`takeWayList` | `services.price_items` | ✅ |
| 7 | `create-order` | **`mcdgit commit` / `merge` 的下单** | `services.place_order` | ✅ |
| 8 | `query-order` | `push` 取支付链接、查支付状态 | `services.order_status` | ✅ |
| 9 | `cancel-order` | `mcdgit revert` 退单 | `services.cancel` | ✅ |
| 10 | `auto-bind-coupons` | `mcdgit pull` 的"同步远端"= 领券进卡包 | `services.bind_coupons` | |

### 已接入但**刻意不用**的 Tool

把这个列表写出来，是因为"没用哪些"和"用了哪些"一样能说明设计意图：

| Tool | 为什么不用 |
|---|---|
| `list-nutrition-foods` | 活动规则明确禁止"宣扬不健康饮食方式"。热量/钠含量一旦露脸就有被解读的风险，而这个项目不需要它 |
| `query-my-coupons` / `query-store-coupons` / `available-coupons` | 省钱的活儿官方示例已经占了，本项目不抢 |
| `query-promotions` / `query-meal-assistance` | 团餐满减规则。本项目的 `merge` 走的是**商品维度**合并，不是**价格维度**，接了会让语义变浑 |
| `order-list` | 历史订单由本地 `objects/` 承担。这是"本地优先"的必然结果：`mcdgit log` 不该需要联网 |
| `campaign-calendar` / `query-survey-coupon` / `query-my-account` | 不在 git 语义的射程内。硬接进来就是凑数 |
| `mall-*` / `party-*` / `query-my-prizes` / `draw-lottery` / `query-lottery-info` | 同上。积分商城、生日派对、抽奖都很好，但它们和"版本控制"没有同构关系 |

---

## 3. 核心调用流程

### 3.1 `mcdgit init` → `remote` 已配置

```text
searchType=1（收藏）
  └─ query-nearby-stores(beType=1)                     # 有收藏直接用
     ↓ 收藏为空 / 指定了 --city 时退回搜索
     query-nearby-stores(beType=1, searchType=2, city, keyword)
     ↓ 用户 --pick N
  写 .mcdgit/config.json: {store_code, store_name, be_type, be_code, address_id}
```

麦乐送 / 团餐走另一条路（因为要先有地址才知道能送到哪些店）：

```text
delivery-query-addresses()          → 取 addressId
delivery-query-stores(beType, addressId)  → 取 storeCode + beCode
```

### 3.2 `mcdgit fetch` —— 拉远端

```text
query-meals(storeCode, orderType, beType)
  └─ {categories:[{name, meals:[{code,tags}]}], meals:{code:{name,currentPrice,originalPrice,image}}}
     ↓ 归一化成 {code: Meal}，价格 "33.5"(元/字符串) → 3350(分/整数)
  写 .mcdgit/cache.json
```

### 3.3 `mcdgit add` —— 局部联网，构造配料快照

这是整个项目最核心的一段，`.mcdgitignore` 就是在这里生效的：

```text
find_meal(本地 cache)                      # 命中就不联网
  └─ query-meal-detail(storeCode, orderType, beType, code)
       └─ modification.items[].values[]
            {code, name, selectedQuantity, selectedKey:"0-1", unselectedKey:"0-0"}
     ↓
  build_mods(meal, overrides):
     ① 默认值 = selectedQuantity
     ② .mcdgitignore 命中  → qty=0, key=unselectedKey   ← 这一步是真会改变外卖内容的
     ③ 命令行 --mod NAME=数量 → qty=N, key="<组号>-N"
     ↓
  暂存为完整快照写进 .mcdgit/index.json
```

### 3.4 `mcdgit diff` —— 唯一的"本地命令却要联网"

```text
calculate-price(items=[<快照转成的 payload>], storeCode, orderType, beType)
  └─ {price, originalPrice, discount, productList:[{productCode, subtotal}], takeWayList}
     ↓
  render: 特调改动画成 +/- 行，每道餐品带回券后小计，末尾给 diffstat 与合计
```

> 为什么要走 `calculate-price` 而不是本地乘加？因为**券后价本地算不出来**。

### 3.5 `mcdgit commit` —— 下单（但还不花钱）

```text
items[].modification.values[] = 该餐品**全部**配料
                                选中 → selectedKey
                                未选中但有 unselectedKey → unselectedKey
                                （官方要求：含 unselectedKey 的组，组内所有项都要传）
create-order(items, takeWayCode, needTableware=false, storeCode, orderType, beType
             [, reservationDate][, remark])
  └─ {orderId, payUrl}
     ↓
  commit.sha = orderId[:7]        ← 提交哈希就是订单号
  写 objects/<sha[:2]>/<sha[2:]>.json，移动 HEAD
```

### 3.6 `mcdgit push` —— 唯一花钱的地方

```text
query-order(orderId) → {payUrl, status}
  ↓
  打印支付链接（拿不到就提示去 App 里付）
  ↓
  用户扫码 → 钱才真的动
```

### 3.7 `mcdgit revert` / `merge` / `clone`

```text
revert:  cancel-order(orderId, cancelReasonCode) + 分支指针退回父提交
merge:   两个分支的 items 求并集
         ├─ 无重叠 → create-order(合并结果) + 双 parent commit
         └─ 有重叠 → MergeConflict（--strategy ours/theirs/union 解决后再走上面）
clone:   本地有 sha → 直接读 objects
         否则当订单号 → query-order → 反解商品列表
```

---

## 4. 关键参数约束（踩坑记录）

这一节是"真的跑过"和"只是读过文档"的分界线。

### 4.1 `orderType` / `beType` / `beCode`

| beType | 场景 | orderType | beCode |
|:---:|---|:---:|---|
| 1 | 到店自取 | 1 | **不传**（传了直接报错） |
| 5 | 得来速 | 1 | **必传**（来自 `query-nearby-stores`） |
| 2 | 麦乐送 | 2 | **必传**（来自 `delivery-query-stores`） |
| 6 | 企业团餐 | 2 | **必传**（来自 `delivery-query-stores`） |

实现在 `model.Store.mcp_args()`，并有测试 `test_be_code_rule_per_be_type` 兜着。

### 4.2 特调 `modification.values[]`

- 传的是**完整快照**，不是差异
- 选中：`key = selectedKey`（`"0-1"`）
- 未选中：`key = unselectedKey`（`"0-0"`），**也必须传**
- 加量：`key = "<组号>-<数量>"`，如 `"0-2"`
- key 的格式是 `<groupIndex>-<quantity>`，`groupIndex` 来自 `modification.items` 的下标

### 4.3 响应格式（最容易翻车的一条）

`content[0].text` 不是纯 JSON，而是三段拼接：

```text
每个门店的基本信息按单行展示：
- **门店名称**: data[].storeName
- **门店编号**: data[].storeCode, 唯一编号，工具入参
{"success":true,"code":200,"message":"请求成功","data":[{...}]}
## 展示结果时，每个门店的 storeCode 和 beCode 必须完整展示，
```

所以：整体 `json.loads` 会失败；"从后往前找最后一个 `{`" 会命中嵌套对象
（`reservationTimeOptions` 就是这么坑过我们一次的）。正确做法是找**信封**——
第一个同时含 `success` 或 `code` 的对象。

实现在 `mcp._extract_json()`，对应测试 `TestExtractJson`。

### 4.4 其它

| 坑 | 处理 |
|---|---|
| 价格单位两套 | `query-meals` 给字符串元（`"33.5"`），`calculate-price` 给分。统一在 `services._yuan_to_fen()` 转成分 |
| **同一个餐品两个价** | `query-meals` 的 `currentPrice` 是**促销/菜单价**，`calculate-price` 才是**实付价**。实测「全明星双人分享餐八件套」菜单价 ¥49.90、实付 ¥117.50。一律以 `calculate-price` 为准，但把落差显式画在 `diff` 里，不能让用户以为被加了价 |
| 工具名 kebab-case | 所有调用点统一用实名 |
| 订单号前 7 位可能重复 | `Repo.short_sha()` 做碰撞退避，撞了就多取几位，实在不行退回 sha1 |
| Windows 控制台 GBK | `__main__._force_utf8()` 在最早入口切 UTF-8；ANSI 颜色靠 `SetConsoleMode` 打开 |
| 业务失败藏在 200 里 | 同时看 `success == false` 和非 0 `code`，抛成 `McpToolError` |

---

## 5. 业务价值

**对用户**：把"点餐"这件一次性的事变成了**可回溯、可复用、可协作**的东西。

- `log` / `blame` / `ingredients` 让"我上次到底吃了什么、什么时候开始不要洋葱的"有据可查
- `cherry-pick` / `clone` 让"再点一次那个组合"变成一条命令，而不是翻订单截图
- `merge` 让多人拼单的**分歧可见**——冲突不会被悄悄抹平，而是要求你明确选 `ours` / `theirs` / `union`
- 本地操作全部离线，只有真的要下单才联网

**对麦当劳**：

- **`.mcdgitignore` 把特调能力从"下单时的一次性选择"变成了"长期偏好"**。
  用户一旦写下"洋葱"，这个偏好就会持续作用在之后的每一次下单上——
  这是原有交互形态（每次都要手动改一次）做不到的。
- `merge` 把团餐(beType=6)的多方协商过程显式化，降低了"点错了又不好意思改"的摩擦。
- 全程只读的工具（`query-meals` / `query-meal-detail` / `calculate-price`）
  被高频调用，说明"看"的需求远大于"买"，而这个项目把"看"做成了零成本、可离线的本地操作。

---

## 6. 降级与容错

| 情况 | 行为 |
|---|---|
| 没配 Token | 只在**真正需要联网**时才报错，并给出申请链接；`status`/`log`/`branch` 等本地命令完全不受影响 |
| Token 失效（401/403） | 抛 `McpAuthError`，提示去控制台重新激活 |
| 门店已打烊 / 无在售餐品 | `fetch_menu` 报"当前没有可取到的在售餐品（可能已打烊）"，不写坏缓存 |
| 连接被对端中断 | 统一收敛成 `McpError`（README 里那条 `WinError 10053`） |
| 收藏门店为空 | **不是错误**，自动退回关键词搜索 |
| 拿不到支付链接 | 不编造，提示用户去麦当劳 App 按订单号支付 |
| 订单号前缀碰撞 | 自动延长取位数，绝不覆盖已有 object |

---

## 7. 可复现性

`tests/mock_mcp.py` 是本仓库自带的**假 MCP 服务**，复刻了真实服务的响应格式与字段结构。
所有写操作（`create-order` / `query-order` / `cancel-order`）的自动化测试都打在它上面，
因此：

- CI 可以直接跑，不会在真实麦当劳系统里留下任何订单
- 解析器、参数规则、合并冲突逻辑都可被回归测试覆盖

```bash
python -m unittest discover -s tests -t .   # 33 个测试
python tools/demo.py --no-color             # 生成 README 里的演示输出
```
