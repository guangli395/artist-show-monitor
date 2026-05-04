# 艺人演出上新监控

自动监控指定艺人的**最新演唱会/巡演/新歌**等动态,发现新增立即记录。

## 能拿到什么

每次运行生成 [`latest.md`](./latest.md) 报告,包含:
- **标题**(演出或动态名称)
- **在售状态**(预售 / 在售 / 演出结束 等,仅 juzimima 源)
- **城市 / 场馆 / 详细地址**
- **演出时间**
- **票价 / 票价区间**
- **原始链接**

## 当前监控

| 数据源 | 拿到什么 | 为什么选它 |
|---|---|---|
| **桔子密码 juzimima** | 结构化演出信息(时间/地址/票价/在售状态) | 静态 HTML,无反爬 |
| **新浪娱乐 tags** | 艺人所有相关新闻/动态(演唱会/巡演/新歌/MV) | 天然按艺人聚合,无反爬 |

不用大麦/微博/抖音,因为反爬太硬,且上面两个源已经足够覆盖"艺人发了什么"这个核心诉求。

---

## 本地跑

```bash
pip install -r requirements.txt
python monitor.py --once           # 跑一轮看输出
python monitor.py --once --ci      # 跑一轮 + 生成 latest.md
python monitor.py                  # 常驻后台, 按 config.json 间隔轮询
```

改 `config.json` 里的 `keywords` 和 `sina_artists` 加/减艺人:

```json
{
  "keywords": ["王一博", "蔡徐坤", "周杰伦"],
  "sina_artists": {
    "王一博": "wangyibo",
    "蔡徐坤": "caixukun",
    "周杰伦": "zhoujielun"
  }
}
```

(`sina_artists` 的值是新浪娱乐聚合页 URL 里 `star_` 后面的拼音。打开 `https://tags.sina.com.cn/star_xxx` 能访问就对了。)

---

## 部署到 GitHub Actions (免费, 24×7 自动跑)

项目已经自带 `.github/workflows/monitor.yml`,每 **30 分钟** 自动跑一次,结果提交回仓库。
你只需要做 4 步:

### 1. 创建 GitHub 仓库 (3 分钟)

- 去 <https://github.com> 注册/登录
- 右上角 **New repository**
- 名字随便取(例如 `artist-show-monitor`)
- **Visibility 选 Public**(公共仓库的 Actions 无限免费;私有仓库每月 2000 分钟额度,也够用)
- 不要勾 `Initialize this repository with a README`
- 点 **Create repository**

### 2. 把本地代码 push 上去 (3 分钟)

在本项目目录 (`artist-show-monitor`) 打开 PowerShell,复制你仓库上的指引,大致是:

```powershell
# 第一次需要 (如果没配过 git 身份)
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"

# 在项目目录里
git init
git add .
git commit -m "初始化: 艺人演出上新监控"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

第一次 push 的时候浏览器会弹出来让你登录 GitHub 授权,授权后自动完成。

### 3. 开启 Actions 写入权限 (1 分钟)

这是**最容易漏掉的一步**。Actions 默认不能 commit 回仓库,要手动给权限:

- 打开你的 GitHub 仓库页
- **Settings** → **Actions** → **General**
- 滑到底,找到 **Workflow permissions**
- 选择 **Read and write permissions** (允许写入)
- 勾选 **Allow GitHub Actions to create and approve pull requests**
- 点 **Save**

### 4. 手动触发第一次运行 (30 秒)

- 打开仓库的 **Actions** 标签
- 左边栏点 **Artist Show Monitor**
- 右边 **Run workflow** 下拉 → **Run workflow** 绿色按钮
- 30 秒到 2 分钟后,首次运行会完成,仓库里会多出 `seen.json` 和 `latest.md`

之后就会每 30 分钟自动跑一次,你随时打开仓库看 [`latest.md`](./latest.md) 就是最新报告。

---

## 改艺人列表

直接在 GitHub 网页上改 `config.json` 即可:

1. 打开仓库里的 `config.json`
2. 右上角铅笔图标 ✏️ **Edit**
3. 改 `keywords` 和 `sina_artists`
4. 下方 **Commit changes**

保存后下一次定时触发就会用新配置跑。

---

## 加钉钉/微信/邮箱推送 (可选, 之后再加)

当前版本只把发现写进 `latest.md`,没有主动推送。如果要加:
- 钉钉: `monitor.py` 的 `print_show` 函数旁加一段 HTTP POST
- 微信: 用 [PushPlus](https://www.pushplus.plus/) 等免费服务
- 邮箱: GitHub Actions 里加一步 `actions/send-mail`
- Bark (iOS): `curl https://api.day.app/xxx/标题/内容`

这些需要再 ~30 分钟工作量,跟我说要哪种。

---

## 常见问题

**Q: Actions cron 不准时?**
A: GitHub 官方是 "best-effort",高峰期可能延迟 10-30 分钟。对"上新通知"这种场景完全够用。

**Q: 仓库 commit 历史会爆炸?**
A: 只在有新发现时才会 commit。没新增就跳过(看 workflow 最后一步)。

**Q: 私有仓库可以吗?**
A: 可以。但私有仓库 Actions 有每月 2000 分钟额度(单次 <1 分钟,够你跑几百次)。公共仓库完全无限免费。

**Q: 新浪娱乐可能没收录我关心的艺人?**
A: 在浏览器打开 `https://tags.sina.com.cn/star_{拼音}` 测一下,打得开就有数据。没有就在 `config.json` 的 `sina_artists` 里删掉他。

**Q: 本机运行没问题,Actions 上出错?**
A: 看仓库 **Actions** 标签 → 最近一次 run → 查具体日志。最常见是"Workflow permissions 没开写权限"(第 3 步)。

---

## 目录结构

```
artist-show-monitor/
├── monitor.py              # 主程序
├── config.json             # 艺人/关键词/数据源配置
├── requirements.txt        # 依赖(只需 requests)
├── .gitignore
├── .github/
│   └── workflows/
│       └── monitor.yml     # GitHub Actions 定时调度
├── seen.json               # (自动生成) 已见演出 ID 记录
├── latest.md               # (自动生成) 最新监控报告
└── README.md
```

## 免责

仅用于个人信息订阅。数据来源于 juzimima.com 和 tags.sina.com.cn 的公开页面,遵守 robots.txt,请合理控制请求频率。
