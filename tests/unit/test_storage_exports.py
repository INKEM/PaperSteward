"""仓储导出清单守护：模块类定义、各仓储模块 __all__ 与 storage 再导出三处一致。"""

import inspect
from types import ModuleType

import papersteward.storage as storage
import papersteward.storage.insights as insights_storage
import papersteward.storage.repositories as repositories

_REPOSITORY_MODULES: tuple[ModuleType, ...] = (repositories, insights_storage)


def _declared_repositories() -> set[str]:
    declared: set[str] = set()
    for module in _REPOSITORY_MODULES:
        declared |= set(module.__all__)
    return declared


def _defined_repositories() -> set[str]:
    classes: set[str] = set()
    for module in _REPOSITORY_MODULES:
        classes |= {
            name
            for name, obj in vars(module).items()
            if inspect.isclass(obj) and name.endswith("Repository") and not name.startswith("_")
        }
    return classes


def test_every_public_repository_class_is_exported() -> None:
    missing = _defined_repositories() - _declared_repositories()
    assert not missing, f"仓储类未列入所在模块 __all__：{sorted(missing)}"


def test_storage_reexports_subset_of_repositories_all() -> None:
    exported = {name for name in storage.__all__ if name.endswith("Repository")}
    assert exported <= _declared_repositories(), (
        f"storage.__init__ 再导出了仓储模块 __all__ 之外的仓储："
        f"{sorted(exported - _declared_repositories())}"
    )


def test_storage_reexports_cover_all_repositories() -> None:
    exported = {name for name in storage.__all__ if name.endswith("Repository")}
    assert exported == _declared_repositories()
