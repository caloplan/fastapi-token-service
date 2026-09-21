"""测试公共夹具：内存 FakeMetaClient（对齐 MetaClient 接口）。"""

from app.clients.meta_client import MetaApiError
from app.services.token_service import TYPE_QUOTA, TYPE_USAGE


class FakeMetaClient:
    """内存版 MetaClient：{type_name:{entity_key: entry}}，模拟 read-modify-write。"""

    def __init__(self, broken: bool = False) -> None:
        self.store: dict[str, dict[str, dict]] = {}
        self.broken = broken

    async def create_entry(self, type_name, entity_key, data, *, user_token):
        if self.broken:
            raise MetaApiError("boom")
        self.store.setdefault(type_name, {})[entity_key] = {"data": dict(data)}
        return {"data": dict(data)}

    async def get_entry(self, type_name, entity_key, *, user_token):
        if self.broken:
            raise MetaApiError("boom")
        entry = self.store.get(type_name, {}).get(entity_key)
        if entry is None:
            return None
        return {"data": dict(entry["data"])}

    async def update_entry(self, type_name, entity_key, data, *, user_token):
        if self.broken:
            raise MetaApiError("boom")
        self.store.setdefault(type_name, {})[entity_key] = {"data": dict(data)}
        return {"data": dict(data)}


__all__ = ["FakeMetaClient", "TYPE_USAGE", "TYPE_QUOTA"]
