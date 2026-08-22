"""Immich-API-Client (getestet gegen Immich 3.1.0)."""

import logging
import os
import time

import requests

log = logging.getLogger("immich")

# Codecs, die ein MP4-Container direkt aufnehmen kann. Alles andere muss
# beim Muxen umkodiert werden - alte 3GP-Dateien tragen z.B. AMR-NB.
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}


class ImmichError(RuntimeError):
    pass


class Immich:
    def __init__(self, base_url, api_key, timeout=120):
        # Sowohl "http://host:2283" als auch ".../api" akzeptieren.
        base = base_url.rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        self.base = base
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})

    # -- intern ---------------------------------------------------------

    def _request(self, method, path, **kw):
        url = f"{self.base}/api{path}"
        kw.setdefault("timeout", self.timeout)
        last = None
        for attempt in range(3):
            try:
                r = self.session.request(method, url, **kw)
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code < 400:
                return r
            # 5xx sind es wert, wiederholt zu werden; 4xx nicht.
            if r.status_code < 500:
                raise ImmichError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            last = ImmichError(f"{method} {path} -> {r.status_code}")
            time.sleep(2 * (attempt + 1))
        raise ImmichError(f"{method} {path} endgueltig fehlgeschlagen: {last}")

    # -- oeffentlich ----------------------------------------------------

    def ping(self):
        """Verbindung und Schluessel pruefen, gibt (version, benutzername) zurueck."""
        v = self._request("GET", "/server/version").json()
        version = f"{v.get('major')}.{v.get('minor')}.{v.get('patch')}"
        try:
            me = self._request("GET", "/users/me").json()
            who = me.get("email") or me.get("name") or "?"
        except ImmichError:
            who = "?"
        return version, who

    def iter_videos(self, page_size=1000, max_pages=200):
        """Alle Video-Assets ueber ALLE Seiten liefern.

        Der Vorgaenger las nur die erste Seite und sah damit von mehreren
        tausend Videos nur die neuesten paar hundert.
        """
        page = 1
        seen = 0
        while page and page <= max_pages:
            body = {"type": "VIDEO", "size": page_size, "page": page, "withExif": True}
            data = self._request("POST", "/search/metadata", json=body).json()
            assets = data.get("assets", {})
            items = assets.get("items", [])
            for it in items:
                seen += 1
                yield it
            nxt = assets.get("nextPage")
            # nextPage kommt als String zurueck, nicht als Zahl.
            if nxt in (None, "", 0, "0"):
                break
            page = int(nxt)
        log.info("Immich lieferte %d Videos", seen)

    def asset_detail(self, asset_id):
        return self._request("GET", f"/assets/{asset_id}").json()

    def resolve_source(self, asset):
        """Bei gestapelten Assets die beste Quelle im Stapel bestimmen.

        Ein Suchtreffer kann ein beliebiges Mitglied eines Stapels sein - auch
        ein bereits bearbeitetes oder eine kleinere Fassung. Bearbeitet werden
        soll aber immer die groesste Datei, und das Ergebnis gehoert an
        denselben Stapel.

        Rueckgabe: (Quell-Asset, Stapel-Elternteil, Ueberspringgrund oder None)
        """
        stack = asset.get("stack") or {}
        parent_id = stack.get("primaryAssetId") or asset.get("stackParentId")
        if not stack and not parent_id:
            return asset, asset["id"], None

        primary = parent_id or asset["id"]
        try:
            parent = self.asset_detail(primary)
        except ImmichError:
            return asset, asset["id"], None

        members = [parent]
        children = parent.get("stack") or []
        if isinstance(children, list):
            members.extend(children)
        elif isinstance(children, dict) and children.get("assets"):
            members.extend(children["assets"])

        best, best_size = asset, 0
        for member in members:
            name = (member.get("originalFileName") or "")
            if "_boosted" in name:
                return asset, primary, "Stapel enthaelt bereits eine bearbeitete Fassung"
            size = int((member.get("exifInfo") or {}).get("fileSizeInByte") or 0)
            if size > best_size:
                best_size, best = size, member
        return best, primary, None

    def download_original(self, asset_id, dest_path):
        url = f"{self.base}/api/assets/{asset_id}/original"
        with self.session.get(url, stream=True, timeout=self.timeout) as r:
            if r.status_code != 200:
                raise ImmichError(f"Download {asset_id} -> {r.status_code}")
            tmp = dest_path + ".part"
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
            os.replace(tmp, dest_path)
        return dest_path

    def upload(self, path, asset):
        """Bearbeitete Datei hochladen, Zeitstempel des Originals uebernehmen."""
        data = {
            "deviceAssetId": f"{asset.get('deviceAssetId') or asset['id']}-boosted",
            "deviceId": asset.get("deviceId") or "immich-booster",
            "fileCreatedAt": asset["fileCreatedAt"],
            "fileModifiedAt": asset["fileModifiedAt"],
            "isFavorite": "false",
        }
        with open(path, "rb") as fh:
            files = {"assetData": (os.path.basename(path), fh, "video/mp4")}
            r = self._request("POST", "/assets", files=files, data=data)
        body = r.json()
        new_id = body.get("id")
        if not new_id:
            raise ImmichError(f"Upload ohne Asset-ID: {body}")
        return new_id, body.get("status")

    def stack(self, parent_id, child_id):
        """Beide Assets stapeln. Immich 3.x: POST /api/stacks.

        Das erste Element der Liste wird zum Elternteil des Stapels. Der alte
        Endpunkt POST /assets/{id}/stack existiert nicht mehr.
        """
        r = self._request("POST", "/stacks", json={"assetIds": [parent_id, child_id]})
        return r.json().get("id")

    def add_tag(self, asset_ids, tag_name):
        """Assets mit einem Tag versehen, damit KI-bearbeitetes Material auffindbar ist."""
        r = self._request("PUT", "/tags", json={"tags": [tag_name]})
        tags = r.json()
        tag_id = None
        for t in tags if isinstance(tags, list) else []:
            if t.get("value") == tag_name or t.get("name") == tag_name:
                tag_id = t.get("id")
                break
        if not tag_id:
            return None
        self._request("PUT", f"/tags/{tag_id}/assets", json={"ids": list(asset_ids)})
        return tag_id
