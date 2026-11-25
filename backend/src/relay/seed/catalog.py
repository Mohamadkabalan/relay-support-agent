"""The Pixel & Volt catalog: 60 fictional products in 10 categories, $9.99 to $2,499.99.

Brands and products are invented. Prices are tax-inclusive USD cents. A few items are refurbished
(15-day return window), a few are final sale, and gift cards and digital products can't be returned.
"""

from dataclasses import dataclass

from relay.domain.enums import ProductCondition, ProductKind

NEW = ProductCondition.NEW
REFURB = ProductCondition.REFURBISHED
PHYSICAL = ProductKind.PHYSICAL


@dataclass(frozen=True, slots=True)
class CatalogItem:
    sku: str
    name: str
    category: str
    price_cents: int
    condition: ProductCondition = NEW
    kind: ProductKind = PHYSICAL
    final_sale: bool = False


CATALOG: tuple[CatalogItem, ...] = (
    # audio
    CatalogItem("AUD-ANC700", "Aurora ANC 700 Headphones", "audio", 34999),
    CatalogItem("AUD-ANC500", "Aurora ANC 500 Headphones", "audio", 22999),
    CatalogItem("AUD-BUDS2", "Aurora Buds 2", "audio", 12999),
    CatalogItem("AUD-BUDSL", "Aurora Buds Lite", "audio", 5999),
    CatalogItem("AUD-SPK-GO", "Tidewave Go Speaker", "audio", 7999),
    CatalogItem("AUD-SPK-MAX", "Tidewave Max Speaker", "audio", 29999),
    CatalogItem("AUD-BAR300", "Tidewave Soundbar 300", "audio", 44999),
    CatalogItem("AUD-ANC700-R", "Aurora ANC 700 Headphones (Refurbished)", "audio", 24999, REFURB),
    # phones
    CatalogItem("PHN-NOVA15", "Nova 15 Smartphone 256 GB", "phones", 89999),
    CatalogItem("PHN-NOVA15P", "Nova 15 Pro Smartphone 512 GB", "phones", 129999),
    CatalogItem("PHN-NOVA14-R", "Nova 14 Smartphone (Refurbished)", "phones", 49999, REFURB),
    CatalogItem("PHN-LUMA-A5", "Luma A5 Smartphone", "phones", 34999),
    CatalogItem("PHN-CASE15", "Nova 15 Clear Case", "phones", 2999),
    CatalogItem("PHN-GLASS15", "Nova 15 Screen Protector", "phones", 1999),
    # computers
    CatalogItem("CMP-AIR14", "Kestrel Air 14 Laptop", "computers", 119999),
    CatalogItem("CMP-PRO16", "Kestrel Pro 16 Laptop", "computers", 249999),
    CatalogItem("CMP-PRO14-R", "Kestrel Pro 14 Laptop (Refurbished)", "computers", 139999, REFURB),
    CatalogItem("CMP-MINI", "Kestrel Mini Desktop", "computers", 69999),
    CatalogItem("CMP-MON27", "Clearview 27-inch 4K Monitor", "computers", 39999),
    CatalogItem(
        "CMP-MON32-OB", "Clearview 32-inch Monitor (Open Box)", "computers", 44999, final_sale=True
    ),
    CatalogItem("CMP-KB-MX", "Strata Mechanical Keyboard", "computers", 12999),
    CatalogItem("CMP-MOUSE", "Strata Wireless Mouse", "computers", 4999),
    # tablets
    CatalogItem("TAB-SLATE11", "Slate 11 Tablet 128 GB", "tablets", 49999),
    CatalogItem("TAB-SLATE13", "Slate 13 Tablet 256 GB", "tablets", 79999),
    CatalogItem("TAB-READ7", "Pageturn 7 E-Reader", "tablets", 13999),
    CatalogItem("TAB-PEN2", "Slate Pen 2", "tablets", 9999),
    CatalogItem("TAB-SLATE11-R", "Slate 11 Tablet (Refurbished)", "tablets", 34999, REFURB),
    # wearables
    CatalogItem("WRB-PULSE4", "Pulse 4 Smartwatch", "wearables", 29999),
    CatalogItem("WRB-PULSE4S", "Pulse 4 Sport Smartwatch", "wearables", 34999),
    CatalogItem("WRB-BAND3", "Pulse Band 3 Fitness Tracker", "wearables", 8999),
    CatalogItem("WRB-STRAP", "Pulse Sport Strap", "wearables", 3999),
    CatalogItem("WRB-RING", "Pulse Ring", "wearables", 24999),
    # cameras
    CatalogItem("CAM-X100", "Lumen X100 Mirrorless Camera", "cameras", 149999),
    CatalogItem("CAM-LENS50", "Lumen 50 mm f/1.8 Lens", "cameras", 29999),
    CatalogItem("CAM-ACT5", "Driftcam 5 Action Camera", "cameras", 39999),
    CatalogItem("CAM-DRONE", "Skylark Mini Drone", "cameras", 54999),
    CatalogItem("CAM-SD256", "ProCard 256 GB SD Card", "cameras", 4999),
    CatalogItem("CAM-TRIPOD", "Lumen Travel Tripod", "cameras", 8999),
    # smart home
    CatalogItem("HOM-HUB", "Hearth Smart Hub", "smart_home", 12999),
    CatalogItem("HOM-CAM", "Hearth Indoor Camera", "smart_home", 7999),
    CatalogItem("HOM-DOOR", "Hearth Video Doorbell", "smart_home", 17999),
    CatalogItem("HOM-BULB4", "Hearth Smart Bulb 4-Pack", "smart_home", 4999),
    CatalogItem("HOM-PLUG2", "Hearth Smart Plug 2-Pack", "smart_home", 2499),
    CatalogItem("HOM-THERMO", "Hearth Smart Thermostat", "smart_home", 19999),
    CatalogItem("HOM-VAC", "Hearth Robot Vacuum", "smart_home", 39999),
    # accessories
    CatalogItem("ACC-USBC1M", "Volt USB-C Cable 1 m", "accessories", 999),
    CatalogItem("ACC-USBC2M", "Volt USB-C Cable 2 m", "accessories", 1499),
    CatalogItem("ACC-CHG65", "Volt 65 W Charger", "accessories", 4999),
    CatalogItem("ACC-PWR20K", "Volt 20,000 mAh Power Bank", "accessories", 5999),
    CatalogItem("ACC-HUB7", "Volt 7-in-1 USB-C Hub", "accessories", 6999),
    CatalogItem("ACC-SLEEVE14", "Volt Laptop Sleeve 14-inch", "accessories", 3499),
    CatalogItem("ACC-STAND", "Volt Aluminum Laptop Stand", "accessories", 4499),
    CatalogItem("ACC-CLEAN", "Volt Screen Cleaning Kit", "accessories", 1299, final_sale=True),
    # gaming
    CatalogItem("GAM-CONSOLE", "Arcadia One Console", "gaming", 49999),
    CatalogItem("GAM-PAD", "Arcadia Wireless Controller", "gaming", 6999),
    CatalogItem("GAM-HEADSET", "Arcadia Gaming Headset", "gaming", 9999),
    CatalogItem(
        "GAM-CODE-ST", "Starfall Odyssey (Digital Code)", "gaming", 6999, kind=ProductKind.DIGITAL
    ),
    # gift cards and services
    CatalogItem(
        "GFT-25",
        "Pixel & Volt Gift Card $25",
        "gift_cards_and_plans",
        2500,
        kind=ProductKind.GIFT_CARD,
    ),
    CatalogItem(
        "GFT-100",
        "Pixel & Volt Gift Card $100",
        "gift_cards_and_plans",
        10000,
        kind=ProductKind.GIFT_CARD,
    ),
    CatalogItem(
        "SVC-CARE2",
        "VoltCare 2-Year Protection Plan",
        "gift_cards_and_plans",
        7999,
        kind=ProductKind.DIGITAL,
    ),
)

BY_SKU = {item.sku: item for item in CATALOG}
