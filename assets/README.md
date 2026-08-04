# assets/

模型与场景文件。**指针不进 payload**：大文件（外部模型、权重、数据集）不进 git，只保留获取脚本与来源记录。

| 资产 | 获取方式 | 来源 |
|---|---|---|
| `menagerie/<model>/` | `uv run python scripts/fetch_assets.py` | [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie)（SO-ARM101 优先，回退 SO-ARM100；版本记录在 `<model>/.source`） |

自建 MJCF 场景（M1 起）直接放本目录并进 git。
