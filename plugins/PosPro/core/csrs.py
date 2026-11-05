"""NRCan CSRS-PPP: submit a base-station RINEX file, wait, read the result.

Follows NRCan's own csrs_ppp_auto.py (v1.6). The user's e-mail address is
the CSRS-PPP user name; NRCan requires it.
"""
import io
import os
import re
import time
import uuid
import zipfile

from . import products

DOMAIN = "https://webapp.csrs-scrs.nrcan-rncan.gc.ca"



class CsrsError(Exception):
    pass


def _multipart(fields, file_field, file_path):
    boundary = "----PosPro%s" % uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    with open(file_path, "rb") as fh:
        data = fh.read()
    out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
               "Content-Type: text/plain\r\n\r\n" % (boundary, file_field, os.path.basename(file_path))).encode())
    out.write(data)
    out.write(("\r\n--%s--\r\n" % boundary).encode())
    return out.getvalue(), "multipart/form-data; boundary=%s" % boundary


def _post(url, body, ctype):
    try:
        return products.http_post(url, body, ctype, timeout=300,
                                  headers={"User-Agent": "CSRS-PPP access via Python Browser Emulator",
                                           "Accept": "text/plain"}).decode("utf-8", "replace")
    except products.DownloadError as e:
        raise CsrsError(str(e))


def submit(rinex_path, email, frame="NAD83", epoch="CURR", vdatum="cgvd2013", mode="Static",
           log=None, is_cancelled=None, poll_s=10, max_wait_s=3600):
    """Submit, wait and return the parsed summary (dict) plus the zip path."""
    if not email or "@" not in email:
        raise CsrsError("CSRS-PPP needs the e-mail address you use as your NRCan user name")
    fields = {
        "return_email": "dummy_email", "cmd_process_type": "std", "ppp_access": "nobrowser_status",
        "language": "en", "user_name": email, "process_type": mode,
        "sysref": frame, "nad83_epoch": epoch if frame == "NAD83" else "CURR",
        "v_datum": vdatum if frame == "NAD83" else "cgvd2013", "output_pdf": "lite",
    }
    body, ctype = _multipart(fields, "rfile_upload", rinex_path)
    if log:
        log("submitting %s to NRCan CSRS-PPP" % os.path.basename(rinex_path))
    reply = _post(DOMAIN + "/CSRS-PPP/service/submit", body, ctype).strip()
    if "DOCTYPE" in reply or "ERROR" in reply.upper()[:20] or not reply:
        raise CsrsError("CSRS-PPP refused the file: %s" % reply[:300])
    key = reply.split()[0]
    t0 = time.time()
    status = ""
    fails = 0
    while time.time() - t0 < max_wait_s:
        if is_cancelled and is_cancelled():
            raise CsrsError("cancelled")
        time.sleep(poll_s)
        try:
            status = products.http_get(DOMAIN + "/CSRS-PPP/service/results/status?id=%s" % key).decode(
                "utf-8", "replace")
            fails = 0
        except products.DownloadError as e:      # a dropped connection while waiting: ask again
            fails += 1
            if fails >= 6:
                raise CsrsError("lost contact with CSRS-PPP while it processed (job %s): %s" % (
                    key, str(e).splitlines()[0][:200]))
            continue
        if log:
            log("CSRS-PPP: %s" % status.strip()[:80])
        if "done" in status.lower():
            break
        if "error" in status.lower():
            raise CsrsError("CSRS-PPP reported an error: %s" % status.strip()[:300])
    else:
        raise CsrsError("CSRS-PPP did not finish within %d minutes (last status: %s)" % (max_wait_s // 60, status))
    try:
        data = products.http_get(DOMAIN + "/CSRS-PPP/service/results/file?id=%s" % key)
    except products.DownloadError as e:
        raise CsrsError("CSRS-PPP finished but its results could not be downloaded (job %s): %s" % (
            key, str(e).splitlines()[0][:200]))
    out_zip = os.path.splitext(rinex_path)[0] + "_csrs_full_output.zip"
    with open(out_zip, "wb") as fh:
        fh.write(data)
    return parse_zip(out_zip), out_zip


def parse_zip(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        sums = [n for n in names if n.lower().endswith(".sum")]
        if not sums:
            raise CsrsError("no .sum file in the CSRS-PPP result")
        text = z.read(sums[0]).decode("utf-8", "replace")
    return parse_sum(text)


def parse_sum(text):
    """Extract the final coordinates from a CSRS-PPP .sum file.

    Returns {"systems": {SYST: {"xyz": (..), "sigma95": (..), "epoch": "..", "llh": (..)}},
             "antenna": .., "arp_height": ..}
    The ESTIMATED column holds the solution. Lines are matched by tokens,
    not columns, because the layout varies between versions.
    """
    res = {"systems": {}, "antenna": "", "raw": text[-4000:]}
    for line in text.splitlines():
        tok = line.split()
        if not tok:
            continue
        if tok[0] == "ANT" and len(tok) > 1:
            res["antenna"] = " ".join(tok[1:3])
        if tok[0] != "POS" or len(tok) < 6:
            continue
        crd = tok[1]
        if crd not in ("X", "Y", "Z", "LAT", "LON", "HGT"):
            continue
        syst, epoch = tok[2], tok[3]
        entry = res["systems"].setdefault(syst, {"epoch": epoch, "xyz": [None] * 3, "sigma95": [None] * 3,
                                                 "llh": [None] * 3})
        rest = tok[4:]
        if crd in ("X", "Y", "Z"):
            # A_PRIORI ESTIMATED DIFF SIG ...
            try:
                est = float(rest[1])
                sig = float(rest[3]) if len(rest) > 3 else None
            except (ValueError, IndexError):
                continue
            i = "XYZ".index(crd)
            entry["xyz"][i] = est
            entry["sigma95"][i] = sig
        elif crd in ("LAT", "LON"):
            # a priori d m s, estimated d m s, diff, sig
            try:
                d, m, s = float(rest[3]), float(rest[4]), float(rest[5])
                v = abs(d) + m / 60 + s / 3600
                if d < 0 or rest[3].startswith("-"):
                    v = -v
                entry["llh"][0 if crd == "LAT" else 1] = v
            except (ValueError, IndexError):
                pass
        else:
            try:
                entry["llh"][2] = float(rest[1])
            except (ValueError, IndexError):
                pass
    for k in list(res["systems"].keys()):
        if None in res["systems"][k]["xyz"]:
            del res["systems"][k]
    if not res["systems"]:
        raise CsrsError("could not read coordinates from the CSRS-PPP summary")
    return res


def epoch_to_decimal(code):
    """CSRS epoch 'YY:DDD:SSSSS' or 'YYYY-MM-DD' to decimal year."""
    m = re.match(r"(\d\d):(\d\d\d):(\d+)", code or "")
    if m:
        y = 2000 + int(m.group(1))
        return y + (int(m.group(2)) - 1 + int(m.group(3)) / 86400.0) / (366 if y % 4 == 0 else 365)
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)", code or "")
    if m:
        import datetime
        t = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        from . import geo
        return geo.decimal_year(t)
    return None
