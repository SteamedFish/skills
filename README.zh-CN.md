# SteamedFish Skills Catalog

OpenCode v2 skills catalog 仓库：自有 skills 与第三方 skills 的唯一真相源，
经 GitHub Actions 构建后由 GitHub Pages 暴露为 HTTPS catalog。

## Catalog URL

- Global（所有项目默认生效）：`https://steamedfish.github.io/skills/global/`
- Per-project（仅特定项目）：`https://steamedfish.github.io/skills/projects/<name>/`

各项目的 `opencode.json`：

```jsonc
{
  "skills": [
    "https://steamedfish.github.io/skills/global/",
    "https://steamedfish.github.io/skills/projects/route-web/"
  ]
}
```

多个 catalog 在客户端数组合并，不互相覆盖。

## 添加 skill

### 自有 skill

在 `skills/<name>/` 下新建目录，放入 `SKILL.md`（标准 frontmatter：
`name` + `description`），然后在 `catalogs.yaml` 里把它挂到目标 catalog。

### 第三方源

在 `sources.yaml` 里添加 source 条目（`repo` / `ref` / `subdir` / `discover`
glob / `include` 子集），然后在 `catalogs.yaml` 里用 `<source>/<name>` 引用。
GitHub Actions 每日 cron 检测上游变化并自动更新 `vendor/`。

## 管理 UI

route-web 的 `/skills` section 提供 Catalogs 管理（global/project 间移动
skill、编辑 per-catalog metadata override）与 Publish（提交配置、触发
workflow、查看运行状态）入口。

## License

- 自有 skills（`skills/`）以 MIT 发布（见仓库根 `LICENSE`）。
- `vendor/` 下的第三方 skills 保留各自 license（上游 LICENSE 文件随
  vendor 与发布产物一起分发）。

## 构建

```sh
python tooling/build.py sync      # 需要网络：更新 vendor/
python tooling/build.py validate  # 校验引用与 frontmatter
python tooling/build.py build     # 产出 dist/（含 index.json 与 version 哈希）
```

`dist/` 不进 git，由 GitHub Actions 构建并以 Pages artifact 部署。
