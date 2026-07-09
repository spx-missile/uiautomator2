import re
import time
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.request import urlopen
from xml.etree import ElementTree as ET


Bounds = Dict[str, int]


class SpxposedNode:
    def __init__(self, element: ET.Element, parent: Optional["SpxposedNode"], order: int):
        self.element = element
        self.parent = parent
        self.order = order
        self.children: List[SpxposedNode] = []

    @property
    def attrs(self) -> Dict[str, str]:
        return self.element.attrib

    def descendants(self) -> Iterable["SpxposedNode"]:
        for child in self.children:
            yield child
            yield from child.descendants()


class SpxposedSelectorBackend:
    _REL_KEY = "childOrSibling"
    _REL_SELECTOR_KEY = "childOrSiblingSelector"
    _INTERNAL_KEYS = {"mask", _REL_KEY, _REL_SELECTOR_KEY, "instance"}

    def __init__(self, session):
        self.session = session

    def should_use(self) -> bool:
        if self.session.settings["selector_backend"] != "spxposed":
            return False
        try:
            package_name = self.session.app_current().get("package")
        except Exception:
            return False
        return package_name == self.session.settings["spxposed_foreground_package"]

    def dump_hierarchy(self) -> str:
        if self.session.settings["spxposed_dump_transport"] == "adb_shell":
            return self._dump_hierarchy_adb_shell()
        url = self.session.settings["spxposed_dump_url"]
        timeout = self.session.settings["spxposed_dump_timeout"]
        with urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def _dump_hierarchy_adb_shell(self) -> str:
        url = self.session.settings["spxposed_dump_url"]
        timeout = self.session.settings["spxposed_dump_timeout"]
        response = self.session.shell(["curl", "-sS", "--max-time", str(timeout), url], timeout=timeout + 2)
        if response.exit_code != 0:
            raise RuntimeError(f"SPXposed dump command failed: {response.output.strip()}")
        return response.output

    def find(self, selector) -> List[SpxposedNode]:
        roots = self._parse(self.dump_hierarchy())
        nodes = [node for root in roots for node in self._walk(root)]
        matches = self._match_selector(nodes, selector)
        rels = selector.get(self._REL_KEY, [])
        rel_selectors = selector.get(self._REL_SELECTOR_KEY, [])
        for rel, rel_selector in zip(rels, rel_selectors):
            if rel == "child":
                candidates = [desc for node in matches for desc in node.descendants()]
            elif rel == "sibling":
                candidates = []
                for node in matches:
                    if node.parent:
                        candidates.extend([s for s in node.parent.children if s is not node])
            else:
                candidates = []
            matches = self._match_selector(candidates, rel_selector)
        return matches

    def count(self, selector) -> int:
        return len(self.find(selector))

    def info(self, selector) -> Dict:
        node = self._single(selector)
        return self.node_info(node)

    def info_list(self, selector) -> List[Dict]:
        return [self.node_info(node) for node in self.find_without_instance(selector)]

    def find_without_instance(self, selector) -> List[SpxposedNode]:
        clone = selector.clone()
        if "instance" in clone:
            del clone["instance"]
        return self.find(clone)

    def wait(self, selector, exists: bool, timeout: Optional[float]) -> bool:
        deadline = time.time() + (timeout if timeout is not None else self.session.wait_timeout)
        while True:
            found = self.count(selector) > 0
            if found == exists:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.2)

    def get_text(self, selector) -> str:
        return self._single(selector).attrs.get("text", "")

    def node_info(self, node: SpxposedNode) -> Dict:
        attrs = node.attrs
        bounds = self._parse_bounds(attrs.get("bounds", "[0,0][0,0]"))
        return {
            "text": attrs.get("text", ""),
            "className": attrs.get("class", ""),
            "class": attrs.get("class", ""),
            "resourceName": attrs.get("resource-id", ""),
            "resourceId": attrs.get("resource-id", ""),
            "contentDescription": attrs.get("content-desc", ""),
            "packageName": attrs.get("package", ""),
            "bounds": bounds,
            "visibleBounds": bounds,
            "checkable": self._bool(attrs.get("checkable")),
            "checked": self._bool(attrs.get("checked")),
            "clickable": self._bool(attrs.get("clickable")),
            "enabled": self._bool(attrs.get("enabled")),
            "focusable": self._bool(attrs.get("focusable")),
            "focused": self._bool(attrs.get("focused")),
            "longClickable": self._bool(attrs.get("long-clickable")),
            "scrollable": self._bool(attrs.get("scrollable")),
            "selected": self._bool(attrs.get("selected")),
            "password": self._bool(attrs.get("password")),
        }

    def _single(self, selector) -> SpxposedNode:
        matches = self.find(selector)
        if not matches:
            raise IndexError("selector not found")
        return matches[0]

    def _parse(self, xml: str) -> List[SpxposedNode]:
        root = ET.fromstring(xml)
        counter = 0

        def convert(element: ET.Element, parent: Optional[SpxposedNode]) -> SpxposedNode:
            nonlocal counter
            counter += 1
            node = SpxposedNode(element, parent, counter)
            node.children = [convert(child, node) for child in list(element) if child.tag == "node"]
            return node

        return [convert(child, None) for child in list(root) if child.tag == "node"]

    def _walk(self, node: SpxposedNode) -> Iterable[SpxposedNode]:
        yield node
        for child in node.children:
            yield from self._walk(child)

    def _match_selector(self, nodes: Iterable[SpxposedNode], selector) -> List[SpxposedNode]:
        matches = [node for node in nodes if self._matches(node, selector)]
        instance = selector.get("instance")
        if instance is None:
            return matches
        return matches[instance:instance + 1]

    def _matches(self, node: SpxposedNode, selector) -> bool:
        for key, value in selector.items():
            if key in self._INTERNAL_KEYS:
                continue
            if key == "index":
                if int(node.attrs.get("index", -1)) != int(value):
                    return False
            elif key == "text" and node.attrs.get("text", "") != value:
                return False
            elif key == "textContains" and value not in node.attrs.get("text", ""):
                return False
            elif key == "textStartsWith" and not node.attrs.get("text", "").startswith(value):
                return False
            elif key == "textMatches" and not re.match(value, node.attrs.get("text", "")):
                return False
            elif key == "className" and node.attrs.get("class", "") != value:
                return False
            elif key == "classNameMatches" and not re.match(value, node.attrs.get("class", "")):
                return False
            elif key == "description" and node.attrs.get("content-desc", "") != value:
                return False
            elif key == "descriptionContains" and value not in node.attrs.get("content-desc", ""):
                return False
            elif key == "descriptionStartsWith" and not node.attrs.get("content-desc", "").startswith(value):
                return False
            elif key == "descriptionMatches" and not re.match(value, node.attrs.get("content-desc", "")):
                return False
            elif key == "packageName" and node.attrs.get("package", "") != value:
                return False
            elif key == "packageNameMatches" and not re.match(value, node.attrs.get("package", "")):
                return False
            elif key == "resourceId" and not self._resource_id_matches(node.attrs.get("resource-id", ""), value):
                return False
            elif key == "resourceIdMatches" and not self._resource_id_regex_matches(node.attrs.get("resource-id", ""), value):
                return False
            elif key in self._BOOL_ATTRS and self._bool(node.attrs.get(self._BOOL_ATTRS[key])) != bool(value):
                return False
        return True

    _BOOL_ATTRS = {
        "checkable": "checkable",
        "checked": "checked",
        "clickable": "clickable",
        "longClickable": "long-clickable",
        "scrollable": "scrollable",
        "enabled": "enabled",
        "focusable": "focusable",
        "focused": "focused",
        "selected": "selected",
    }

    def _resource_id_matches(self, actual: str, expected: str) -> bool:
        if actual == expected:
            return True
        if actual.endswith(":id/" + expected):
            return True
        if actual.rsplit("/", 1)[-1] == expected:
            return True
        return False

    def _resource_id_regex_matches(self, actual: str, pattern: str) -> bool:
        return bool(re.match(pattern, actual) or re.match(pattern, actual.rsplit("/", 1)[-1]))

    def _parse_bounds(self, value: str) -> Bounds:
        match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value or "")
        if not match:
            return {"left": 0, "top": 0, "right": 0, "bottom": 0}
        left, top, right, bottom = [int(group) for group in match.groups()]
        return {"left": left, "top": top, "right": right, "bottom": bottom}

    def _bool(self, value: Optional[str]) -> bool:
        return value == "true"
