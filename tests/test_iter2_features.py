"""
Iteration-2 backend tests: file uploads, COD, StylePoints redemption,
videos (admin+public), Google session error, sparse phone index,
online payment coexistence with COD.
Run with:  pytest /app/backend/tests/test_iter2_features.py -v -n 0
"""
import io
import os
import random
import struct
import uuid
import zlib

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "realdrizzaro@gmail.com"
ADMIN_PASSWORD = "StyleNow@73419"


def _tiny_png() -> bytes:
    # minimal 1x1 PNG
    sig = b"\x89PNG\r\n\x1a\n"
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


# ---------- Fixtures ----------

@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def customer_session():
    s = requests.Session()
    phone = "9" + "".join(random.choices("0123456789", k=9))
    r = s.post(f"{API}/auth/otp/request", json={"phone": phone}, timeout=30)
    assert r.status_code == 200
    otp = r.json()["dev_otp"]
    r2 = s.post(f"{API}/auth/otp/verify", json={"phone": phone, "otp": otp, "name": "TEST_Iter2"}, timeout=30)
    assert r2.status_code == 200
    s.phone = phone
    s.user_id = r2.json()["user"]["id"]
    return s


@pytest.fixture(scope="module")
def uploaded_image_url(admin_session):
    files = {"file": ("test.png", _tiny_png(), "image/png")}
    r = admin_session.post(f"{API}/admin/upload", files=files, timeout=60)
    assert r.status_code == 200, r.text
    j = r.json()
    return j  # {path, url, kind}


@pytest.fixture(scope="module")
def category(admin_session):
    name = f"TEST_IT2_{uuid.uuid4().hex[:5]}"
    r = admin_session.post(f"{API}/admin/categories", json={"name": name, "sort": 5, "active": True}, timeout=15)
    assert r.status_code == 200
    return r.json()["category"]


@pytest.fixture(scope="module")
def product(admin_session, category, uploaded_image_url):
    # variant with 13 image URLs to test 12 cap
    imgs = [uploaded_image_url["url"]] + [f"https://example.com/img{i}.jpg" for i in range(12)]
    body = {
        "name": f"TEST_IT2 Tee {uuid.uuid4().hex[:4]}",
        "description": "iter2 test product",
        "category_id": category["id"],
        "brand": "StyleNow",
        "gender": "unisex",
        "tags": ["iter2test"],
        "images": [uploaded_image_url["url"]],
        "variants": [
            {"sku": f"IT2-BLK-M-{uuid.uuid4().hex[:4]}", "color": "Black", "size": "M",
             "price": 500, "mrp": 800, "stock": 5, "images": imgs},
        ],
        "featured": False, "active": True,
    }
    r = admin_session.post(f"{API}/admin/products", json=body, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["product"]


@pytest.fixture(scope="module")
def address(customer_session):
    r = customer_session.post(f"{API}/addresses", json={
        "name": "TEST_IT2", "phone": customer_session.phone,
        "line1": "Iter2 Street", "city": "Bahraich", "pincode": "271801",
        "is_default": True}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["address"]


# ---------- Tests ----------

class TestUpload:
    def test_upload_and_serve(self, admin_session, uploaded_image_url):
        j = uploaded_image_url
        assert j["kind"] == "image"
        assert j["url"].startswith("/api/files/")
        # Serve back
        r = requests.get(f"{BASE_URL}{j['url']}", timeout=30)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("image/")
        assert len(r.content) > 0

    def test_upload_unsupported_type(self, admin_session):
        files = {"file": ("bad.txt", b"hello", "text/plain")}
        r = admin_session.post(f"{API}/admin/upload", files=files, timeout=30)
        assert r.status_code == 400


class TestProductImageCap:
    def test_variant_images_capped_at_12(self, product):
        assert len(product["variants"][0]["images"]) == 12


class TestCOD:
    order_id = None

    def test_cod_order_flow(self, customer_session, product, address, admin_session):
        v = product["variants"][0]
        # clear cart
        cart = customer_session.get(f"{API}/cart", timeout=15).json()
        for it in cart.get("items", []):
            customer_session.delete(f"{API}/cart/items/{it['product_id']}/{it['variant_id']}", timeout=15)
        # add to cart
        r = customer_session.post(f"{API}/cart/items",
                                  json={"product_id": product["id"], "variant_id": v["id"], "qty": 1}, timeout=15)
        assert r.status_code == 200
        # quote
        q = customer_session.get(f"{API}/checkout/quote", timeout=15)
        assert q.status_code == 200
        qj = q.json()
        assert qj["subtotal"] == v["price"]
        # Place COD order
        r = customer_session.post(f"{API}/orders",
                                  json={"address_id": address["id"], "payment_method": "cod"}, timeout=30)
        assert r.status_code == 200, r.text
        rj = r.json()
        assert rj["payment"]["mode"] == "cod"
        assert rj["order"]["payment_status"] == "cod"
        assert rj["order"]["payment_method"] == "cod"
        TestCOD.order_id = rj["order"]["id"]
        # Stock deducted
        pd = requests.get(f"{API}/products/{product['id']}", timeout=15).json()["product"]
        v0 = next(x for x in pd["variants"] if x["id"] == v["id"])
        assert v0["stock"] == v["stock"] - 1
        # Admin sees it
        r_admin = admin_session.get(f"{API}/admin/orders", params={"q": TestCOD.order_id}, timeout=15)
        assert r_admin.status_code == 200
        assert any(o["id"] == TestCOD.order_id for o in r_admin.json()["items"])
        # Customer detail shows cod
        det = customer_session.get(f"{API}/orders/{TestCOD.order_id}", timeout=15).json()["order"]
        assert det["payment_status"] == "cod"
        assert det["payment_method"] == "cod"


class TestStylePoints:
    def test_redeem_points_and_cancel_refund(self, admin_session, customer_session, product, address):
        # credit user with 200 points
        r = admin_session.post(f"{API}/admin/customers/{customer_session.user_id}/points",
                               json={"points": 200, "note": "TEST_IT2 grant"}, timeout=15)
        assert r.status_code == 200
        # clear cart, add 1 item (₹500 subtotal → 10% = ₹50 max redemption)
        cart = customer_session.get(f"{API}/cart", timeout=15).json()
        for it in cart.get("items", []):
            customer_session.delete(f"{API}/cart/items/{it['product_id']}/{it['variant_id']}", timeout=15)
        v = product["variants"][0]
        customer_session.post(f"{API}/cart/items",
                              json={"product_id": product["id"], "variant_id": v["id"], "qty": 1}, timeout=15)
        # quote WITHOUT redeem
        q0 = customer_session.get(f"{API}/checkout/quote", timeout=15).json()
        assert q0["points_used"] == 0
        # quote WITH redeem
        q = customer_session.get(f"{API}/checkout/quote", params={"redeem_points": "true"}, timeout=15).json()
        assert q["points_balance"] >= 200
        # subtotal=500, discount=0, max_redeem = 50
        assert q["points_used"] == 50, f"expected 50 got {q['points_used']}"
        assert q["points_discount"] == 50
        assert q["total"] == q0["total"] - 50
        # place COD order with redeem
        r = customer_session.post(f"{API}/orders", json={
            "address_id": address["id"], "payment_method": "cod", "redeem_points": True}, timeout=30)
        assert r.status_code == 200, r.text
        order_id = r.json()["order"]["id"]
        assert r.json()["order"]["points_redeemed"] == 50
        # wallet debited
        rew = customer_session.get(f"{API}/rewards", timeout=15).json()
        bal_after_redeem = rew["wallet"]["balance"]
        # Cancel → refund
        rc = customer_session.post(f"{API}/orders/{order_id}/cancel", timeout=15)
        assert rc.status_code == 200, rc.text
        rew2 = customer_session.get(f"{API}/rewards", timeout=15).json()
        assert rew2["wallet"]["balance"] == bal_after_redeem + 50, \
            f"refund expected +50: before={bal_after_redeem} after={rew2['wallet']['balance']}"


class TestVideos:
    def test_admin_create_and_public_listing(self, admin_session, uploaded_image_url, product):
        # Use uploaded image url as fake video path (endpoint only validates schema fields)
        body = {"username": "TEST_reviewer", "caption": "Great!",
                "product_id": product["id"], "video": uploaded_image_url["url"],
                "kind": "review", "active": True, "sort": 1}
        r = admin_session.post(f"{API}/admin/videos", json=body, timeout=15)
        assert r.status_code == 200, r.text
        vid = r.json()["video"]
        # public /api/videos
        pv = requests.get(f"{API}/videos", timeout=15).json()
        assert any(v["id"] == vid["id"] for v in pv["items"])
        found = next(v for v in pv["items"] if v["id"] == vid["id"])
        assert found.get("product"), "product summary should be attached"
        assert found["product"]["id"] == product["id"]
        # product detail includes videos
        pd = requests.get(f"{API}/products/{product['id']}", timeout=15).json()
        assert any(v["id"] == vid["id"] for v in pd.get("videos", []))
        # cleanup
        admin_session.delete(f"{API}/admin/videos/{vid['id']}", timeout=15)

    def test_video_bad_kind_rejected(self, admin_session, uploaded_image_url):
        r = admin_session.post(f"{API}/admin/videos", json={
            "username": "x", "video": uploaded_image_url["url"], "kind": "not_valid"}, timeout=15)
        assert r.status_code == 400


class TestGoogle:
    def test_invalid_session_returns_401(self):
        r = requests.post(f"{API}/auth/google/session", json={"session_id": "invalid-xyz-123"}, timeout=30)
        assert r.status_code in (401, 502), f"got {r.status_code}: {r.text}"


class TestPhoneSparseIndex:
    def test_two_phoneless_users_allowed(self):
        # Verify via direct pymongo: create two users without phone (email-only, Google-like)
        import motor.motor_asyncio, asyncio  # noqa
        from pymongo import MongoClient
        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME", "test_database")
        if not mongo_url:
            pytest.skip("MONGO_URL not set")
        client = MongoClient(mongo_url)
        db = client[db_name]
        u1 = {"id": "TEST_IT2_G1_" + uuid.uuid4().hex[:6], "email": f"g1_{uuid.uuid4().hex[:6]}@t.com", "auth_provider": "google"}
        u2 = {"id": "TEST_IT2_G2_" + uuid.uuid4().hex[:6], "email": f"g2_{uuid.uuid4().hex[:6]}@t.com", "auth_provider": "google"}
        try:
            db.users.insert_one(u1)
            db.users.insert_one(u2)
        finally:
            db.users.delete_many({"id": {"$in": [u1["id"], u2["id"]]}})
        # Also verify index is sparse
        idx = db.users.index_information()
        phone_idx = next((v for k, v in idx.items() if "phone" in k), None)
        assert phone_idx, f"phone index missing: {idx}"
        assert phone_idx.get("sparse") is True, f"phone index not sparse: {phone_idx}"


class TestOnlinePaymentStillWorks:
    def test_online_and_verify_simulated(self, customer_session, product, address):
        # clear cart, add 1 item
        cart = customer_session.get(f"{API}/cart", timeout=15).json()
        for it in cart.get("items", []):
            customer_session.delete(f"{API}/cart/items/{it['product_id']}/{it['variant_id']}", timeout=15)
        v = product["variants"][0]
        # ensure stock available
        pd = requests.get(f"{API}/products/{product['id']}", timeout=15).json()["product"]
        cur_stock = next(x["stock"] for x in pd["variants"] if x["id"] == v["id"])
        if cur_stock < 1:
            pytest.skip(f"insufficient stock ({cur_stock}) for online payment test")
        customer_session.post(f"{API}/cart/items",
                              json={"product_id": product["id"], "variant_id": v["id"], "qty": 1}, timeout=15)
        r = customer_session.post(f"{API}/orders",
                                  json={"address_id": address["id"], "payment_method": "online"}, timeout=30)
        assert r.status_code == 200, r.text
        rj = r.json()
        assert rj["payment"]["mode"] == "simulated"
        oid = rj["order"]["id"]
        assert rj["order"]["payment_status"] == "pending"
        vp = customer_session.post(f"{API}/orders/{oid}/verify-payment",
                                   json={"simulated": True}, timeout=15)
        assert vp.status_code == 200
        assert vp.json()["payment_status"] == "paid"
