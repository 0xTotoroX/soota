# content/

> L2 | 父级: ../../../AGENTS.md

- `__init__.py`：正文绑定包入口。
- `discovery.py`：运行时能力清单与已知结果契约。
- `native.py`：显式来源与默认采集路由、原生操作及已知正文/媒体结果转换。
- `native_bindings.py`：OpenCLI 结果契约、来源候选和目标身份校验。
- `native_types.py`：原生操作、正文、媒体返回类型。
- `weir.py`：Weir 单帖原文转换、目标校验与全文补取。
- `wechat.py`：微信 DOM/脚本元数据、原顺序 Markdown、懒加载图片/代码块与删除/验证状态判定。

这里只做确定性的来源格式处理，不生成分析、文章或知识流程。微信 HTTP 获取归 providers/wechat.py，稳定身份归 ../wechat.py。

[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
