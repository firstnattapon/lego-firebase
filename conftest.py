from __future__ import annotations

import copy
import sys
import types
import uuid


class FakeReference:
    def __init__(self, root: dict, path: str):
        self.root = root
        self.parts = [p for p in path.split('/') if p]

    def _parent(self, create=True):
        node = self.root
        for part in self.parts[:-1]:
            if create:
                node = node.setdefault(part, {})
            else:
                node = node.get(part)
                if node is None:
                    return None, None
        return node, self.parts[-1] if self.parts else None

    def get(self):
        node = self.root
        for part in self.parts:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return copy.deepcopy(node)

    def set(self, value):
        if not self.parts:
            self.root.clear()
            self.root.update(copy.deepcopy(value or {}))
            return
        parent, key = self._parent(True)
        parent[key] = copy.deepcopy(value)

    def update(self, fields):
        current = self.get() or {}
        current.update(copy.deepcopy(fields))
        self.set(current)

    def delete(self):
        if not self.parts:
            self.root.clear()
            return
        parent, key = self._parent(False)
        if parent is not None:
            parent.pop(key, None)

    def transaction(self, fn):
        current = self.get()
        result = fn(copy.deepcopy(current))
        self.set(result)
        return copy.deepcopy(result)

    def push(self, value):
        current = self.get() or {}
        key = uuid.uuid4().hex
        current[key] = copy.deepcopy(value)
        self.set(current)
        return types.SimpleNamespace(key=key)


class FakeDB:
    def __init__(self):
        self.store = {}

    def reference(self, path=''):
        return FakeReference(self.store, path)


FAKE_DB = FakeDB()

firebase_admin = types.ModuleType('firebase_admin')
firebase_admin._apps = []
firebase_admin.initialize_app = lambda *args, **kwargs: firebase_admin._apps.append(object())
firebase_admin.credentials = types.SimpleNamespace(ApplicationDefault=lambda: object())
firebase_admin.db = FAKE_DB
sys.modules.setdefault('firebase_admin', firebase_admin)
sys.modules.setdefault('firebase_admin.db', FAKE_DB)

ff = types.ModuleType('functions_framework')
ff.http = lambda fn: fn
sys.modules.setdefault('functions_framework', ff)
