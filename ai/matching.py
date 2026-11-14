"""
AI matching engine for Campus Lost & Found.

TEXT MATCHING  : TF-IDF vectors + cosine similarity (scikit-learn)
IMAGE MATCHING : perceptual hash (ImageHash). If ImageHash is not installed,
                 a small average-hash built with Pillow is used instead.

IMPORTANT LIMITS (also shown to users in the website):
  * Scores only SUGGEST a potential match. They are never proof of ownership.
  * Image hashes are NOT semantic recognition. Angle, crop or lighting changes
    lower the similarity.
  * Only authorised college staff can verify ownership.
"""
from PIL import Image, ImageOps
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

try:
    import imagehash
    HAVE_IMAGEHASH = True
except ImportError:  # the app still works without it
    imagehash = None
    HAVE_IMAGEHASH = False

# Fields compared for text matching and how many times each is repeated
# (a simple way of giving important fields more weight).
FIELD_WEIGHTS = [
    ("name", 3),
    ("category", 2),
    ("color", 2),
    ("brand", 2),
    ("distinctive_features", 2),
    ("description", 1),
    ("location", 1),
]

CATEGORY_MISMATCH_FACTOR = 0.5   # text score is halved when categories differ
TEXT_WEIGHT = 0.7                # used when both reports have photos
IMAGE_WEIGHT = 0.3
IMAGE_DISTANCE_LIMIT = 32        # 64-bit hash: distance >= 32 means "not similar"
MIN_SCORE = 0.20                 # chat opens only for matches of 20% or more
MAX_RESULTS = 10


def build_text(item):
    """Turn an item (dict) into one lowercase string for TF-IDF."""
    parts = []
    for field, weight in FIELD_WEIGHTS:
        value = (item.get(field) or "").strip().lower()
        if value:
            parts.extend([value] * weight)
    if (item.get("college_logo") or "").lower() == "yes":
        parts.append("collegelogo")
    return " ".join(parts)


def compute_image_hash(path):
    """Return a hash string like 'p:ab12...' or None if the image can't be read."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            if HAVE_IMAGEHASH:
                return "p:" + str(imagehash.phash(img, hash_size=8))
            small = img.convert("L").resize((8, 8), Image.LANCZOS)
            pixels = list(small.getdata())
            avg = sum(pixels) / len(pixels)
            bits = "".join("1" if p > avg else "0" for p in pixels)
            return "a:" + format(int(bits, 2), "016x")
    except Exception:
        return None


def image_similarity(hash_a, hash_b):
    """0.0 - 1.0 similarity, or None when the two hashes can't be compared."""
    if not hash_a or not hash_b:
        return None
    kind_a, _, value_a = hash_a.partition(":")
    kind_b, _, value_b = hash_b.partition(":")
    if kind_a != kind_b:
        return None
    distance = bin(int(value_a, 16) ^ int(value_b, 16)).count("1")
    return max(0.0, 1.0 - distance / IMAGE_DISTANCE_LIMIT)


def find_matches(target, candidates, corpus):
    """
    Rank `candidates` (list of item dicts) against `target` (item dict).

    `corpus` is every item in the database. TF-IDF needs it so that words that
    are common across the whole campus (e.g. "bottle") count for less than rare
    words (e.g. a sticker description).

    Returns a list of dicts: {id, text_score, image_score, score}, best first.
    """
    if not candidates:
        return []

    documents = [build_text(item) for item in corpus] or [build_text(target)]
    documents = [d for d in documents if d] or ["empty"]
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", sublinear_tf=True)
    try:
        vectorizer.fit(documents)
        target_vec = vectorizer.transform([build_text(target)])
        cand_vecs = vectorizer.transform([build_text(c) for c in candidates])
    except ValueError:  # empty vocabulary
        return []
    similarities = cosine_similarity(target_vec, cand_vecs).flatten()

    results = []
    for cand, text_score in zip(candidates, similarities):
        text_score = float(text_score)
        if (cand.get("category") or "") != (target.get("category") or ""):
            text_score *= CATEGORY_MISMATCH_FACTOR
        img_score = image_similarity(target.get("image_hash"), cand.get("image_hash"))
        if img_score is None:
            final = text_score
        else:
            final = TEXT_WEIGHT * text_score + IMAGE_WEIGHT * img_score
        if final >= MIN_SCORE:
            results.append({
                "id": cand["id"],
                "text_score": round(text_score, 4),
                "image_score": None if img_score is None else round(img_score, 4),
                "score": round(final, 4),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:MAX_RESULTS]
