<div align="center">

# mcd-git

**把麦当劳点餐变成版本控制。**

`git commit` 就是下单，`git blame` 能查出是谁加的洋葱。

<sub>零依赖 · 只用 Python 标准库 · `git clone` 完就能跑</sub>

</div>

---

```console
$ mcdgit blame 洋葱粒
+ 1030102  10-10 16:50  引入 洋葱粒   「feat: 试一口洋葱」
- 1030103  10-10 16:50  移除 洋葱粒   「fix: 戒洋葱」
+ 1030104  10-10 16:50  加回 洋葱粒   「feat: 复吸」

  ← 上次你明明删掉了，这次又是为什么？
```

## 这不是套壳

一开始只是想玩个梗，但把麦当劳 MCP 的返回结构摊开之后发现，**订单和 git 的语义是同构的**，不是硬凑：

| git | mcd-git | 为什么天生对得上 |
|---|---|---|
| `commit` | `create-order` | 提交 = 下单 |
| `commit hash` | **麦当劳订单号前 7 位** | 不是哈希出来的，俩就是同一个东西 |
| `push` | 扫码支付 | 本地动作 vs 影响远端 |
| `diff` | `calculate-price` + 特调快照 | 加菜前后 = 改动前后 |
| **`.gitignore`** | `modification.values[].unselectedKey` | **"忽略洋葱" = 让麦当劳别放洋葱** |
| `remote` | 门店 | 远端 = 一家店 |
| `log` | `order-list` + `query-order` | 提交历史 = 点餐历史 |
| `merge` | 团餐 `beType=6` | 多人分支合并 = 拼单，抢最后一份鸡翅 = **冲突** |
| `clone` | 别人的订单号 | 把订单号发出去，对方一条命令复刻 |
| `revert` | `cancel-order` | 回滚 = 退单 |

**`.mcdgitignore` 是真的会生效的。** 这不是装饰——写在里面的配料，会被翻译成 `create-order` 请求体里的 `unselectedKey`，改变麦当劳实际做出来的东西。整个项目里最实在的一个映射，也是最容易被忽略的那个。

## 30 秒上手

不需要 `pip install`，没有 `requirements.txt`：

```bash
git clone https://github.com/<you>/mcd-git
cd mcd-git
export MCD_MCP_TOKEN=<你的 Token>      # https://open.mcd.cn/mcp → 控制台 → 激活

python mcdgit init --city 南京市 --keyword 鼓楼   # 选一家店
python mcdgit fetch                               # 拉菜单
python mcdgit add 巨无霸 --mod 吉士=2              # 加进暂存区
python mcdgit diff                                # 看看改了什么、多少钱
python mcdgit commit -m "feat: 下午续命"           # 下单（**还不花钱**）
python mcdgit push                                # 扫码支付
```

> 想先看看它长什么样、又不想掏钱？`python tools/demo.py` 会跑一段完整会话，
> 全程打在本仓库自带的假 MCP 服务上，**不会产生任何真实订单**。

---

## 三个名场面

### ① `diff` —— 特调就是文件里的行

```console
$ mcdgit diff
diff --mcdgit a/巨无霸 b/巨无霸
--- a/巨无霸
+++ b/巨无霸
@@ 巨无霸 特调 @@
+  吉士 1 -> 2
-  洋葱粒 1 -> 0   ← .mcdgitignore
  ×1 → ¥26.00

──────────────────────────────────────────────────────────────
 2 files changed, 2 insertions(+), 1 deletion(-)
 合计 ¥40.50
```

### ② `blame` —— 追溯一个配料的来去

见页首。它的价值不在"谁写的"（作者永远是你自己），而在**"这一行是什么时候又跑回来的"**。

### ③ `merge` —— 团餐拼单冲突

```console
$ mcdgit merge alice
fatal: CONFLICT (content): Merge conflict in 麦辣鸡腿堡

        alice: 我要
        bob: 我要

        解决：--strategy ours（我点的） / --strategy theirs（他点的） / --strategy union（都点上）
```

```console
$ mcdgit log --graph --oneline
* 1030107 (HEAD -> main) (merge) merge: main ← alice
* | 1030106 feat(bob): 这个鸡腿堡也是我的
| * 1030105 feat(alice): 这个鸡腿堡是我的
|/
* 1030104 feat: 复吸
* 1030103 fix: 戒洋葱
* 1030102 feat: 试一口洋葱
* 1030101 feat: 下午续命
```

完整会话见 [`docs/demo.txt`](./docs/demo.txt)（由 `tools/demo.py` 生成，改渲染逻辑后可以重新生成，不会过期）。

---

## 两个安全设计

**1. `commit` 不花钱，`push` 才花钱。**
`create-order` 只创建订单、返回支付链接，不扫码就不扣钱。所以"本地 / 远端"这套心智模型在这里**是字面成立**的，不是比喻。这也是为什么 `commit` 敢在 `--help` 里被写成"提交"。

**2. 本地操作一个字节都不上云。**

```console
$ mcdgit status / log / blame / branch / stash / tag / reset    # 0 次网络请求
$ mcdgit fetch / commit / push / revert                          # 才碰 MCP
```

`diff` 是唯一的例外——它要调 `calculate-price` 才知道券后价。所以你不会因为"看一眼暂存区"就把门店信息发出去。

---

## 命令一览

<details>
<summary>展开全部 27 个命令</summary>

| 命令 | 真调 MCP | 说明 |
|---|:---:|---|
| `init` | ✓ | 初始化仓库并选门店（到店/得来速/麦乐送/团餐） |
| `remote` | ✓ | 查看/更换远端门店 |
| `config` | — | 读写本地配置 |
| `fetch` / `pull` | ✓ | 拉菜单；`pull` 顺带 `auto-bind-coupons` 领券 |
| `menu` / `mods` / `stores` | ✓ | 看菜单、看某道餐品的可特调项、搜门店 |
| `status` | — | 暂存区 |
| `add` | ✓ | 加餐品（**幂等**，重复 add 是更新不是又点一份） |
| `rm` | — | 取消暂存，或去掉某个配料 |
| `diff` | ✓ | 特调改动 + 券后价 |
| `reset` / `stash` / `branch` / `checkout` / `tag` | — | 纯本地 |
| `commit` | ✓ | 下单 |
| `push` | ✓ | 支付 |
| `revert` | ✓ | 退单 + 回退分支 |
| `cherry-pick` | — | 把历史某单里的餐品再点一次 |
| `merge` | ✓ | 团餐拼单（`--strategy ours/theirs/union`） |
| `clone` | ✓ | 复刻别人的订单（给订单号即可） |
| `log` / `show` | — | 历史（`--graph` 画提交图） |
| `blame` | — | 追溯配料 |
| `ingredients` | — | 你的配料史记 |

</details>

### `.mcdgitignore`

语法照抄 gitignore，但忽略的是**配料**：

```gitignore
洋葱
酸黄瓜
辣*        # 支持通配
```

匹配比真 gitignore 宽松一点：大小写不敏感、子串命中（写"洋葱"，"洋葱粒"也会中）。
想临时吃一次：`mcdgit add 巨无霸 --mod 洋葱粒=1`（显式指定优先级最高）。

---

## 实现上踩过的坑

写这个项目时最耗时间的不是功能，是**读对官方文档**和**摸清返回格式**。几个值得单独记下来的：

| 坑 | 真相 |
|---|---|
| `query-nearby-stores` 返回的是什么 | 不是纯 JSON，而是 **"给 LLM 的字段说明 + 一段内嵌 JSON + 尾部提醒"**。整体 `json.loads` 会失败，从后往前找 `{` 会命中嵌套对象。正确做法是找**信封**：第一个含 `success` / `code` 的对象 |
| 到店自取要不要传 `beCode` | **不要**，传了会被直接拒绝。但得来速/麦乐送/团餐**必须**传 |
| 特调要传什么 | 不是"你改了什么"，而是**整道餐品的全部配料**。文档原话：*"对于包含 unselectedKey 的特调组，该组内所有特调项都必须传入"* |
| 价格单位 | `calculate-price` 返回的是**分**，而 `query-meals` 给的是**字符串元**（`"33.5"`） |
| **同一个餐品两个价** | `query-meals` 的 `currentPrice` 是促销/菜单价，`calculate-price` 才是实付价。实测「全明星双人分享餐八件套」菜单价 ¥49.90、实付 ¥117.50。以实付为准，但落差要显式画出来 |
| 订单号能不能当 hash | 前 7 位可能撞车——真实订单号共享很长的前缀。所以 `short_sha` 有碰撞退避：撞了就往多取几位 |
| 工具名 | 服务端是 kebab-case（`query-meals`），不是 snake_case |

完整清单见 [`MCP_INTEGRATION.md`](./MCP_INTEGRATION.md)。

---

## 项目结构

```text
mcd-git/
├── README.md                     # 本文件
├── SKILL.md                      # Skill 定义（触发条件 + 工作流 + 铁律）
├── MCP_INTEGRATION.md            # MCP Server / Tool / 调用流程 / 业务价值
├── mcp-config.example.json       # 脱敏配置（仅环境变量占位符）
├── CONTEST_DECLARATION.md        # 参赛声明（官方原文，未改动）
├── mcdgit                        # 启动器（免安装）
├── src/mcdgit/
│   ├── mcp.py                    # MCP streamable-http 客户端 + 响应解析
│   ├── store.py                  # 本地仓库（objects / refs / index / .mcdgitignore）
│   ├── model.py                  # 领域对象
│   ├── services.py               # 所有 MCP 交互都收在这里
│   ├── render.py                 # 渲染（中文双宽对齐、提交图车道算法）
│   └── commands/                 # setup / worktree / order / history
├── tests/
│   ├── mock_mcp.py               # 自带假 MCP 服务（复刻真实响应格式）
│   └── test_mcdgit.py            # 38 个测试
├── tools/demo.py                 # 生成 docs/demo.txt
├── docs/demo.txt                 # 完整演示会话（由上面那个脚本生成）
└── .github/workflows/test.yml    # CI：只跑 mock，不产生任何真实订单
```

### 测试

```bash
python -m unittest discover -s tests -t .
```

**38 个测试，全部打在 `tests/mock_mcp.py` 上——一个本仓库自带的假 MCP 服务。**

这不是偷懒，是必须的：`create-order` 会真的在麦当劳系统里建订单，自动化测试不能干这种事。假服务刻意复刻了真实服务那些反直觉的行为（内嵌 JSON、`success`/`code` 信封、`selectedKey`/`unselectedKey`），所以解析器一旦被"优化"坏，测试会立刻红。

其中几条是专门盯着官方文档里最容易读漏的规则写的：

- `test_dine_in_never_sends_be_code` —— 到店自取不许带 `beCode`
- `test_all_mods_are_sent_not_just_changes` —— 必须传完整配料快照
- `test_ignore_rules_become_unselected_key` —— `.mcdgitignore` 必须落到 `unselectedKey`

### 对真实服务的验证状态

| 类别 | 工具 | 状态 |
|---|---|---|
| 只读 | `query-nearby-stores` `query-meals` `query-meal-detail` `calculate-price` | ✅ 已对真实服务跑通 |
| 写 | `create-order` `query-order` `cancel-order` | 🧪 仅由 mock 覆盖（不拿真店做自动化测试） |

---

## 边界与声明

- 本项目为**麦当劳程序员节创意开发大赛参赛作品**，由参赛者独立开发，**非麦当劳官方产品**。
- `commit` 会真的创建订单（未支付），`push` 会真的扣钱。**请自己确认金额和门店。**
- 餐品信息、价格、供应状态一律以麦当劳官方渠道的实时结果为准。
- 项目输出仅供参考，不构成医疗、营养或其他专业建议。
- Token 只从环境变量或你本机已有的 MCP 客户端配置里读，**不写入任何文件**。
