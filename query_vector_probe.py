"""Can a hosted service make the query vector of lanterN's search, with the same model the passages were embedded with?

The semantic index holds passages embedded with intfloat/multilingual-e5-small (semantic_index.py). A search needs the
vector of the words typed, made by that same model. Today the browser makes it (a download of about 120 MB). This probe
asks Hugging Face's hosted inference for the vector of a few questions, times each call, and compares each vector with
the one the index's own model makes on this machine (cosine: 1.0 means the same direction, so the same neighbours).
It reads and writes nothing in any database. It prints status codes, times and cosines only; never the token.

Usage: HF_TOKEN=... python query_vector_probe.py [--local]   (--local also loads the model here to compare)
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

MODEL = "intfloat/multilingual-e5-small"
URL = "https://router.huggingface.co/hf-inference/models/" + MODEL + "/pipeline/feature-extraction"
QUESTIONS = [
    "how many people work at the company",
    "cuántas personas trabajan en la empresa",
    "total greenhouse gas emissions scope 1 and 2",
    "salario digno para proveedores",
    "politique de droits humains dans la chaîne d'approvisionnement",
    "收入总额",
]


def post(url, token, payload, timeout=30):
    """(status, body text). An HTTP error is returned as its status, not raised."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                   # network down, timeout
        return 0, type(e).__name__


def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def as_vector(body):
    """The one vector in a reply: a flat list, a list holding one list, or token vectors (averaged). None when the reply is not vectors."""
    try:
        d = json.loads(body)
    except ValueError:
        return None
    while isinstance(d, list) and len(d) == 1 and isinstance(d[0], list):
        d = d[0]
    if isinstance(d, list) and d and all(isinstance(x, (int, float)) for x in d):
        return unit(d)
    if isinstance(d, list) and d and all(isinstance(r, list) and r and all(isinstance(x, (int, float)) for x in r) for r in d):
        return unit([sum(col) / len(d) for col in zip(*d)])   # one vector a token: the mean, as the index's model pools
    return None


def cosine(a, b):
    return sum(x * y for x, y in zip(unit(a), unit(b)))


def hosted_vector(text, token, send=post):
    """(vector or None, status, seconds). The e5 models want "query: " before a question."""
    t0 = time.time()
    status, body = send(URL, token, {"inputs": "query: " + text, "normalize": True})
    return (as_vector(body) if status == 200 else None), status, time.time() - t0


def probe(token, questions=QUESTIONS, local=None, send=post, out=print):
    """Ask for each question's vector; with `local` (a function text -> vector) compare. True when every call gave a 384 vector
    and, when compared, every cosine is at least 0.99."""
    good = True
    for i, q in enumerate(questions, 1):
        v, status, s = hosted_vector(q, token, send)
        if v is None or len(v) != 384:
            out(f"question {i}: HTTP {status}, no usable vector ({'length ' + str(len(v)) if v else 'none'}), {s:.2f} s")
            good = False
            continue
        line = f"question {i}: HTTP 200, 384 numbers, {s:.2f} s"
        if local:
            c = cosine(v, list(local(q)))
            line += f", cosine with the index's model {c:.4f}"
            good = good and c >= 0.99
        out(line)
    out("RESULT: " + ("the hosted service returns the index's vectors" if good and local else
                      "the hosted service answers with vectors of the right size" if good else
                      "the hosted service does not serve this model as needed"))
    return good


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--local", action="store_true", help="also load the model here and compare the vectors")
    a = ap.parse_args(argv)
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("HF_TOKEN is not set: nothing asked.")
        return 1
    local = None
    if a.local:
        import semantic_index
        local = semantic_index.Embedder().embed_query
    return 0 if probe(token, local=local) else 1


if __name__ == "__main__":
    sys.exit(main())
