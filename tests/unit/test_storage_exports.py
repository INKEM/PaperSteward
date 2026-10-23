"""仓储导出清单守护：模块类定义、repositories.__all__ 与 storage 再导出三处一致。"""

import inspect

import papersteward.storage as storage
import papersteward.storage.repositories as repositories


def test_every_public_repository_class_is_exported() -> None:
    classes = {
        name
        for name, obj in vars(repositories).items()
        if inspect.isclass(obj) and name.endswith("Repository") and not name.startswith("_")
    }
    missing = classes - set(repositories.__all__)
    assert not missing, f"仓储类未列入 __all__：{sorted(missing)}"


def test_storage_reexports_subset_of_repositories_all() -> None:
    exported = {name for name in storage.__all__ if name.endswith("Repository")}
    assert exported <= set(repositories.__all__), (
        f"storage.__init__ 再导出了 repositories.__all__ 之外的仓储："
        f"{sorted(exported - set(repositories.__all__))}"
    )


def test_storage_reexports_cover_all_repositories() -> None:
    exported = {name for name in storage.__all__ if name.endswith("Repository")}
    assert set(repositories.__all__) == exported
