"""
StyleNow E2E backend tests.
Covers: auth (OTP + admin), catalog CRUD, search + synonyms + suggestions,
cart, coupons, addresses, checkout, order placement (simulated payment),
inventory deduction / overselling, admin order fulfillment, rewards + spin,
wishlist, reviews (buyer gating), homepage ticker CRUD, spin rewards CRUD,
customers list, settings, synonyms CRUD, audit logs, search analytics.
Uses public REACT_APP_BACKEND_URL and cookie-based auth.
"""
import os
import time
import uuid
import random
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # frontend/.env holds the prod-facing URL. Read directly as fallback.
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
    except Exception:
        pass
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "realdrizzaro@gmail.com"
ADMIN_PASSWORD = "StyleNow@73419"


# ---------- Fixtures ----------

@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="session")
def customer_session():
    """OTP-authenticated customer session"""
    s = requests.Session()
    # Random Indian-looking 10-digit phone starting with 9 to avoid collision
    phone = "9" + "".join(random.choices("0123456789", k=9))
    r = s.post(f"{API}/auth/otp/request", json={"phone": phone}, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("sent") is True
    otp = data.get("dev_otp")
    assert otp, "dev_otp missing in dev mode response"
    r2 = s.post(f"{API}/auth/otp/verify", json={"phone": phone, "otp": otp, "name": "TEST Customer"}, timeout=30)
    assert r2.status_code == 200, r2.text
    s.phone = phone
    s.user_id = r2.json()["user"]["id"]
    return s


@pytest.fixture(scope="session")
def created_category(admin_session):
    name = f"TEST_Cat_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{API}/admin/categories", json={"name": name, "sort": 1, "active": True}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["category"]


@pytest.fixture(scope="session")
def created_product(admin_session, created_category):
    body = {
        "name": f"TEST_Oversized Tee {uuid.uuid4().hex[:4]}",
        "description": "Comfortable oversized cotton tee",
        "category_id": created_category["id"],
        "brand": "StyleNow",
        "gender": "unisex",
        "tags": ["oversized", "baggy", "cotton", "tee"],
        "images": ["https://example.com/tee.jpg"],
        "variants": [
            {"sku": "TEE-BLK-M", "color": "Black", "size": "M", "price": 799, "mrp": 999, "stock": 5},
            {"sku": "TEE-WHT-L", "color": "White", "size": "L", "price": 849, "mrp": 999, "stock": 3},
        ],
        "featured": True, "active": True,
    }
    r = admin_session.post(f"{API}/admin/products", json=body, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()["product"]


# ---------- Public config / homepage ----------

class TestPublic:
    def test_config(self):
        r = requests.get(f"{API}/config", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["brand"] == "StyleNow"
        assert d["payment_mode"] == "simulated"
        assert d["free_delivery"] is True

    def test_homepage(self):
        r = requests.get(f"{API}/homepage", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d.get("ticker"), list) and len(d["ticker"]) >= 1
        assert "sections" in d and "categories" in d


# ---------- Auth ----------

class TestAuth:
    def test_otp_invalid_phone(self):
        r = requests.post(f"{API}/auth/otp/request", json={"phone": "123"}, timeout=15)
        assert r.status_code == 400

    def test_otp_request_and_verify(self):
        s = requests.Session()
        phone = "9" + "".join(random.choices("0123456789", k=9))
        r = s.post(f"{API}/auth/otp/request", json={"phone": phone}, timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["sent"] and j.get("dev_otp") and j.get("dev_mode") is True
        # wrong otp
        r_wrong = s.post(f"{API}/auth/otp/verify", json={"phone": phone, "otp": "000000"}, timeout=15)
        assert r_wrong.status_code == 400
        # correct otp
        r_ok = s.post(f"{API}/auth/otp/verify", json={"phone": phone, "otp": j["dev_otp"], "name": "TEST X"}, timeout=15)
        assert r_ok.status_code == 200
        u = r_ok.json()["user"]
        assert u["phone"] == phone
        # cookie set
        assert "sn_token" in s.cookies.get_dict()
        # me
        me = s.get(f"{API}/auth/me", timeout=15)
        assert me.status_code == 200

    def test_admin_login_wrong_password(self):
        r = requests.post(f"{API}/admin/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-pass"}, timeout=15)
        assert r.status_code == 401

    def test_admin_login_success(self, admin_session):
        r = admin_session.get(f"{API}/admin/auth/me", timeout=15)
        assert r.status_code == 200
        assert r.json()["admin"]["email"] == ADMIN_EMAIL


# ---------- Catalog ----------

class TestCatalog:
    def test_create_category_and_product(self, created_category, created_product):
        assert created_category["id"]
        assert len(created_product["variants"]) == 2

    def test_product_visible_public(self, created_product):
        r = requests.get(f"{API}/products", timeout=15)
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()["items"]]
        assert created_product["id"] in ids

    def test_product_detail(self, created_product):
        r = requests.get(f"{API}/products/{created_product['id']}", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert d["product"]["id"] == created_product["id"]
        assert len(d["product"]["variants"]) == 2

    def test_categories_list(self, created_category):
        r = requests.get(f"{API}/categories", timeout=15)
        assert r.status_code == 200
        ids = [c["id"] for c in r.json()["items"]]
        assert created_category["id"] in ids


# ---------- Search ----------

class TestSearch:
    def test_search_by_tag(self, created_product):
        r = requests.get(f"{API}/search", params={"q": "baggy"}, timeout=15)
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()["items"]]
        assert created_product["id"] in ids, f"'baggy' should find product; got {r.json()}"

    def test_search_by_synonym(self, created_product):
        r = requests.get(f"{API}/search", params={"q": "loose fit"}, timeout=15)
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()["items"]]
        assert created_product["id"] in ids, "synonym 'loose fit' → 'oversized' should match"

    def test_search_suggestions(self, created_product):
        r = requests.get(f"{API}/search/suggestions", params={"q": "over"}, timeout=15)
        assert r.status_code == 200
        assert "suggestions" in r.json()


# ---------- Cart & Coupons ----------

@pytest.fixture(scope="session")
def coupon_code(admin_session):
    code = f"TEST{uuid.uuid4().hex[:5].upper()}"
    body = {"code": code, "label": "Test 10% off", "type": "percent", "value": 10,
            "min_order": 0, "per_user_limit": 5, "active": True}
    r = admin_session.post(f"{API}/admin/coupons", json=body, timeout=15)
    assert r.status_code == 200, r.text
    return code


class TestCart:
    def test_add_update_remove(self, customer_session, created_product):
        v = created_product["variants"][0]
        r = customer_session.post(f"{API}/cart/items",
                                  json={"product_id": created_product["id"], "variant_id": v["id"], "qty": 1},
                                  timeout=15)
        assert r.status_code == 200
        cart = r.json()
        assert len(cart["items"]) == 1
        # update qty
        r2 = customer_session.put(f"{API}/cart/items/{created_product['id']}/{v['id']}",
                                  json={"qty": 2}, timeout=15)
        assert r2.status_code == 200
        assert r2.json()["items"][0]["qty"] == 2
        # remove
        r3 = customer_session.delete(f"{API}/cart/items/{created_product['id']}/{v['id']}", timeout=15)
        assert r3.status_code == 200
        assert len(r3.json()["items"]) == 0

    def test_apply_coupon(self, customer_session, created_product, coupon_code):
        v = created_product["variants"][0]
        customer_session.post(f"{API}/cart/items",
                              json={"product_id": created_product["id"], "variant_id": v["id"], "qty": 1}, timeout=15)
        r = customer_session.post(f"{API}/cart/coupon", json={"code": coupon_code}, timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["coupon_code"] == coupon_code
        assert j["discount"] > 0

    def test_generate_coupon_code(self, admin_session):
        r = admin_session.post(f"{API}/admin/coupons/generate", timeout=15)
        assert r.status_code == 200
        assert r.json()["code"].startswith("STYLE")


# ---------- Address / Checkout / Order ----------

@pytest.fixture(scope="session")
def address(customer_session):
    r = customer_session.post(f"{API}/addresses", json={
        "name": "TEST Customer", "phone": customer_session.phone,
        "line1": "1 Test Street", "city": "Bahraich", "pincode": "271801",
        "is_default": True}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["address"]


class TestCheckoutOrder:
    order_id_holder = {}

    def test_quote_and_place_order(self, customer_session, created_product, address, coupon_code):
        v = created_product["variants"][0]
        # ensure exactly 1 item in cart
        # clear
        cart = customer_session.get(f"{API}/cart", timeout=15).json()
        for it in cart.get("items", []):
            customer_session.delete(f"{API}/cart/items/{it['product_id']}/{it['variant_id']}", timeout=15)
        customer_session.post(f"{API}/cart/items",
                              json={"product_id": created_product["id"], "variant_id": v["id"], "qty": 1}, timeout=15)

        q = customer_session.get(f"{API}/checkout/quote", timeout=15)
        assert q.status_code == 200, q.text
        qj = q.json()
        assert qj["delivery_fee"] == 0
        assert qj["subtotal"] == v["price"]

        r = customer_session.post(f"{API}/orders",
                                  json={"address_id": address["id"], "coupon_code": ""}, timeout=15)
        assert r.status_code == 200, r.text
        rj = r.json()
        assert rj["payment"]["mode"] == "simulated"
        order_id = rj["order"]["id"]
        TestCheckoutOrder.order_id_holder["id"] = order_id

        # verify payment simulated
        vp = customer_session.post(f"{API}/orders/{order_id}/verify-payment",
                                   json={"simulated": True}, timeout=15)
        assert vp.status_code == 200
        assert vp.json()["payment_status"] == "paid"

        # order visible in my_orders
        mo = customer_session.get(f"{API}/orders", timeout=15).json()
        assert any(o["id"] == order_id for o in mo["items"])

    def test_inventory_deducted(self, customer_session, created_product):
        pd = requests.get(f"{API}/products/{created_product['id']}", timeout=15).json()["product"]
        v0 = next(v for v in pd["variants"] if v["id"] == created_product["variants"][0]["id"])
        # started at 5, one order for qty 1
        assert v0["stock"] == 4, f"expected 4, got {v0['stock']}"

    def test_overselling_prevented(self, customer_session, created_product, address):
        # try to order more than stock of variant2 (stock=3)
        v2 = created_product["variants"][1]
        # clear cart
        cart = customer_session.get(f"{API}/cart", timeout=15).json()
        for it in cart.get("items", []):
            customer_session.delete(f"{API}/cart/items/{it['product_id']}/{it['variant_id']}", timeout=15)
        # 10 is capped to 10 in add; but stock is 3 → should reject at add
        r = customer_session.post(f"{API}/cart/items",
                                  json={"product_id": created_product["id"], "variant_id": v2["id"], "qty": 10},
                                  timeout=15)
        assert r.status_code == 400, "should reject qty>stock"


# ---------- Admin order fulfillment ----------

class TestAdminOrders:
    def test_status_transitions_and_rider_note(self, admin_session, customer_session, created_product):
        order_id = TestCheckoutOrder.order_id_holder.get("id")
        assert order_id, "prior order test must run first"
        for st in ["confirmed", "preparing", "packed", "out_for_delivery", "delivered"]:
            r = admin_session.put(f"{API}/admin/orders/{order_id}/status", json={"status": st}, timeout=15)
            assert r.status_code == 200, f"{st}: {r.text}"
        # rider
        rr = admin_session.post(f"{API}/admin/orders/{order_id}/rider",
                                json={"name": "TEST Rider", "phone": "9990001111"}, timeout=15)
        assert rr.status_code == 200
        nr = admin_session.post(f"{API}/admin/orders/{order_id}/notes",
                                json={"note": "delivered on time"}, timeout=15)
        assert nr.status_code == 200

        # customer order tracking shows timeline
        detail = customer_session.get(f"{API}/orders/{order_id}", timeout=15).json()["order"]
        timeline_statuses = [t["status"] for t in detail["timeline"]]
        for expected in ["placed", "delivered"]:
            assert expected in timeline_statuses

    def test_admin_orders_list_counts(self, admin_session):
        r = admin_session.get(f"{API}/admin/orders", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert "counts" in j and j["counts"].get("delivered", 0) >= 1


# ---------- Rewards & Spin ----------

class TestRewards:
    def test_points_credited_after_delivery(self, customer_session):
        # order total was 799, points = floor(799*0.05) = 39
        r = customer_session.get(f"{API}/rewards", timeout=15)
        assert r.status_code == 200
        bal = r.json()["wallet"]["balance"]
        assert bal >= 1, f"points should be credited, got {bal}"

    def test_admin_adjust_points_and_spin(self, admin_session, customer_session):
        # top up to 50+
        r = admin_session.post(f"{API}/admin/customers/{customer_session.user_id}/points",
                               json={"points": 100, "note": "TEST"}, timeout=15)
        assert r.status_code == 200
        info = customer_session.get(f"{API}/spin", timeout=15).json()
        assert info["can_spin"] is True
        spin = customer_session.post(f"{API}/spin", timeout=15)
        assert spin.status_code == 200, spin.text
        j = spin.json()
        assert "result" in j and "label" in j["result"]


# ---------- Wishlist ----------

class TestWishlist:
    def test_add_remove(self, customer_session, created_product):
        r = customer_session.post(f"{API}/wishlist", json={"product_id": created_product["id"]}, timeout=15)
        assert r.status_code == 200
        wl = customer_session.get(f"{API}/wishlist", timeout=15).json()
        assert any(p["id"] == created_product["id"] for p in wl["items"])
        r2 = customer_session.delete(f"{API}/wishlist/{created_product['id']}", timeout=15)
        assert r2.status_code == 200


# ---------- Reviews ----------

class TestReviews:
    def test_non_buyer_blocked(self, created_product):
        # brand new customer, hasn't purchased this product
        s = requests.Session()
        phone = "9" + "".join(random.choices("0123456789", k=9))
        j = s.post(f"{API}/auth/otp/request", json={"phone": phone}, timeout=15).json()
        s.post(f"{API}/auth/otp/verify", json={"phone": phone, "otp": j["dev_otp"], "name": "TEST NB"}, timeout=15)
        r = s.post(f"{API}/products/{created_product['id']}/reviews",
                   json={"rating": 5, "comment": "great"}, timeout=15)
        assert r.status_code == 403


# ---------- Admin misc ----------

class TestAdminMisc:
    def test_homepage_ticker_crud(self, admin_session):
        r = admin_session.post(f"{API}/admin/homepage/ticker",
                               json={"text": "TEST TICKER", "active": True, "sort": 99}, timeout=15)
        assert r.status_code == 200
        did = r.json()["deal"]["id"]
        # verify appears on public homepage
        hp = requests.get(f"{API}/homepage", timeout=15).json()
        assert any(d["text"] == "TEST TICKER" for d in hp["ticker"])
        # delete
        rd = admin_session.delete(f"{API}/admin/homepage/ticker/{did}", timeout=15)
        assert rd.status_code == 200

    def test_spin_reward_crud(self, admin_session):
        r = admin_session.post(f"{API}/admin/spin/rewards",
                               json={"label": "TEST Reward", "type": "points", "value": 10,
                                     "probability": 1, "expiry_days": 0, "active": True}, timeout=15)
        assert r.status_code == 200
        rid = r.json()["reward"]["id"]
        rd = admin_session.delete(f"{API}/admin/spin/rewards/{rid}", timeout=15)
        assert rd.status_code == 200

    def test_customers_list_shows_otp_user(self, admin_session, customer_session):
        r = admin_session.get(f"{API}/admin/customers", params={"q": customer_session.phone}, timeout=15)
        assert r.status_code == 200
        assert any(u["phone"] == customer_session.phone for u in r.json()["items"])

    def test_settings_save(self, admin_session):
        r = admin_session.put(f"{API}/admin/settings",
                              json={"delivery_fee": 0, "low_stock_threshold": 5}, timeout=15)
        assert r.status_code == 200
        assert r.json()["settings"]["low_stock_threshold"] == 5

    def test_synonyms_crud(self, admin_session):
        kw = f"testkw{uuid.uuid4().hex[:4]}"
        r = admin_session.post(f"{API}/admin/synonyms",
                               json={"keyword": kw, "synonyms": ["foo", "bar"]}, timeout=15)
        assert r.status_code == 200
        sid = r.json()["synonym"]["id"]
        rd = admin_session.delete(f"{API}/admin/synonyms/{sid}", timeout=15)
        assert rd.status_code == 200

    def test_audit_logs(self, admin_session):
        r = admin_session.get(f"{API}/admin/audit-logs", timeout=15)
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_search_analytics(self, admin_session):
        r = admin_session.get(f"{API}/admin/analytics/search", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert "popular" in j and "zero_results" in j
