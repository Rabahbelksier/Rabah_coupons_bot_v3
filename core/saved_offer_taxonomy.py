"""Exact AliExpress subcategory names used by saved-offer category filters."""


def _names(value):
    return tuple(name.strip() for name in value.split("|") if name.strip())


SAVED_OFFER_SUBCATEGORIES_BY_FILTER = {
    "saved_electronics": _names(
        """
Emergency Safety Supplies | Access Building Automation | Video Surveillance |
Smart Public Safety Systems | Lighting Accessories | Indoor Lighting |
Other Lights & Lighting Products | Smart Lighting | Special Engineering Lighting |
Professional Light | Holiday Lighting | Outdoor Lighting | Lighting Bulbs & Tubes |
Commercial Lighting | Novelty Lighting（new） | Night Lights | LED Lighting |
Portable Lighting | Smart Electronics | Electronic Cigarettes | Games & Accessories |
Portable Audio & Video | Camera & Photo | Home Audio & Video | Walkie Talkie |
Communication Equipment
"""
    ),
    "saved_computer": _names(
        """
Desktops & AIO | Laptops | Office Software | Internal Storage |
Computer Peripherals | Computer Components | Storage Device | Networking |
Computer Cleaners | Computer & Office Bundle | Barebone & Mini PC |
3D Printing & Additive Manufacturing | Office Electronics |
Servers & Industrial Computer | DIY Gaming Computer | Software & Games |
Software &Games (without VAT)
"""
    ),
    "saved_children": _names(
        """
Stress Relief Toy | Play Vehicles & Models | Hobby & Collectibles |
High Tech Toys | ACG Goods | Arts & Crafts | DIY toys | Pools & Water Fun |
Kid's Party | Games and Puzzles | Dolls & Stuffed Toys | Trendy Blind Box |
Electronic Toys | Stuffed Animals & Plush | Remote Control Toys |
Dolls & Accessories | Novelty & Gag Toys | Building & Construction Toys |
Outdoor Fun & Sports | Learning & Education | Pretend Play | Classic Toys |
Baby & Toddler Toys | Action & Toy Figures | Baby Clothing |
Children's Clothing | Baby Strollers&Accessories | Activity & Gear | Feeding |
Safety | Diapering & Toilet Training | Pregnancy & Maternity |
Baby Diaper & Wipes | Car Seats & Accessories | Baby Care | Kids Shoes |
Maternity Clothings | Baby Sterilization & Appliances | Baby Souvenirs |
Baby Furniture | Baby Food | Bedding | Kids Accessories
"""
    ),
    "saved_home": _names(
        """
Other Home Appliances | Major Appliances | Household Appliances |
Personal Care Appliances | Commercial Appliances | Cleaning Appliances |
Kitchen Appliances | Electrical Equipment & Supplies | Hardware |
Quarry Stone & Slabs | Other Home Improvement | Plumbing |
Family Intelligence System | Installation Service |
Garden Landscaping & Decking | Painting Supplies & Wall Treatments |
Functional Material | Metal Building Materials | Bathroom Fixture |
Kitchen Fixture | Heating, Cooling & Vents | Boards |
Doors, Gates & Windows | Building Supplies | Garden Supplies | Home Textile |
Home Storage & Organization | Home Decor | Household Merchandises |
Kitchen,Dining & Bar | Collectibles | Arts,Crafts & Sewing |
Festive & Party Supplies | Pet Products | Furniture Parts |
Furniture Accessories | Café Furniture | Senior Furniture | Home Furniture |
Office Furniture | Commercial Furniture | Outdoor Furniture | Other Furniture |
Bar Furniture | Children Furniture
"""
    ),
    "saved_sports": _names(
        """
Team Sports | Basketball（New） | Football（New） | Sports Competitions |
Racquet Sports | Entertainment | Skiing & Snowboarding | Roller,Skateboard |
Cycling | Other Sports & Entertainment Product | Sports Bags(hidden) |
Water Sports | Shooting | Hunting | Fishing | Horse Riding | Camping & Hiking |
Musical Instruments | Fitness & Body Building | Golf | Cheerleading |
Sportswear | Children's Sports | Dance | Sneakers | Sports Accessories | Sport Bags
"""
    ),
    "saved_beauty": _names(
        """
Sanitary Paper | Oral Hygiene | Skin Care | Makeup | Shaving & Hair Removal |
Beauty Equipment | Rehabilitation Supplies | Medical Laboratory Equipment |
Tattoo & Body Art | Sex Products | Health Care | Bath & Shower |
Fragrances & Deodorants | Tools & Accessories | Hair Care & Styling |
Nail Art & Tools | Dental Supplies | Massage & Relaxation | Skin Care Tool |
Perfume | Beauty Supply | Hair Salon Supply | Hair Tools & Accessories |
Human Hair (For White) | Synthetic Hair | Synthetic Hair(For White) |
Hair For Asian | Human Hair (For Black) | Blend Wigs（For White） |
Human Wigs( For Black)
"""
    ),
    "saved_accessories": _names(
        """
Sportswears | Fashion Jewelry | Customized Jewelry | Jewelry Making |
Fine Jewelry | Jewelry Packaging & Display | Jewelry Tools & Equipments |
Smart Jewelry | Customized Watches | Men's Watches | Women's Watches |
Children's Watches | Couple Watches | Watches Accessories |
Pocket & Fob Watches | Summer Bags | Travel Accessories | Winter Bags |
Travel Bags | Wallets & Holders | Special Purpose Bags | Leisure Bags |
Luggage | Waist Packs | Women's Handbags | Men's Bags | Chest Bags |
Kids' Bags | School Bags | Organizer Bag | Luggage & Travel Bags |
Business Commuter Laptop Bag | Bag Parts & Accessories |
Other Luggage & Bags | Backpack | Scarf | Hat & Glove Sets |
Suspenders/Braces & Posture Correctors | Apparel Fabrics & Textiles |
new Scarf &Wrap | Oversleeve & Arm Warmer | New Headwear | Pocket Squares |
Eyewear & Accessories | Headwear | Gloves & Mittens | Earmuffs |
Handkerchiefs | Scarves & Wraps | Arm Warmers | Hats & Caps | Belts |
Cummerbunds | Knee Sleeve & Leg Warmer | Neck Wear | Belt Buckle |
Mask（New） | Fashionable Canes | DIY Accessories
"""
    ),
    "saved_fashion": _names(
        """
Party & Vacation Wear | Haute Couture Dresses | Wedding Party Dress |
Special Occasion Dresses | Wedding Accessories | Wedding Dresses |
Women's Shoes | Men's Shoes | Mules & Clogs | Shoe Accessories | Other Shoes |
Men Socks | Women's Sleep & Lounge | Bikinis | Men's Underwears |
Men's Sleep & Lounge | Women's Intimates | Women's Socks & Hosiery | Skirts |
Matching Sets | Jeans（New） | Shirts & Blouses | Basic Clothing |
Genuine Leather | Sweaters&Jumpers | Customized Skirts |
Customized Blouses & Shirts | Customized Dresses | Jumpsuits&Rompers |
Down Coats | Swimwears | Parkas | Blazer & Suits | Fur & Faux Fur |
Faux Leather | Women's Sets | Ready-to-wear Dresses |
Jumpsuits, Playsuits & Bodysuits | Shorts | Pants & Capris | Sweaters |
Hoodies & Sweatshirts | Dresses | Blouses & Shirts | Leggings | Tops & Tees |
Coats & Jackets | Real Fur | Plus Size Clothes | Muslim Fashion |
Traditional Women's Clothing | Sweaters | Denim（New） | Basic Clothing |
Plus Size Men's Clothing | Pants | Down Coats | Swimwears | Parkas |
Suits & Blazer | Hoodies & Sweatshirts | Tops & Tees | Coats & Jackets |
Men's Shirts | Tailor-made Shirts |
Tailor-made Hoodies & Sweatshirts | Shorts | Men's Sets（new） |
Traditional Men's Clothing | Middle East Fashion | Functional Apparel |
Cosplay Costumes | Exotic Apparel | Work Wear & Uniforms | World Apparel |
Cosplay Accessories | Stage & Dance Wear
"""
    ),
    "saved_vehicles": _names(
        """
Auto Sale | Car Services | Car Wash & Maintenance | Car Repair Tool |
Exterior Accessories | Interior Accessories | Electrical Equipment |
Car Electronics | Car Maintenance Tools | Modification&Protection |
Aircraft | Boats | Motorcycle & ATV | Motorcycle Equipments |
Motorcycle Accessories | Ornamental & Cleaning
"""
    ),
    "saved_tools": _names(
        """
Power Tools | Welding & Soldering Supplies | Welding Equipment & Supplies |
Measurement & Analysis Instruments | Riveter Guns |
Abrasive Tools & Abrasives | Drill Bits | Saw Blades & Cutting Tools |
Laser Engraving Machine & Accessories | Hand Tools | Construction Tools |
Other Tools | Garden Tools | Tool Sets | Tools Packaging
"""
    ),
    "saved_spare_parts": _names(
        """
Home Appliance Parts | Laptop Parts & Accessories |
New Energy Vehicle Parts & Accessories | Car Lights | Chassis Parts |
Engines & Engine Parts | Wear Parts | Exterior Parts | Auto Replacement Parts |
Car Lock System | Automotive Sensors | Other Vehicle Parts & Accessories |
RV Parts & Accessories | Interior Parts | Mobile Phone Parts | Tool Parts |
Power Tool Parts & Accessories | Industrial Spare Parts | Motorcycle Parts
"""
    ),
    "saved_electronic_supplies": _names(
        """
Tablet Accessories & Parts | Accessories & Parts | Electronic Data Systems |
Electronics Stocks | Active Components | Electronics Production Machinery |
Electronic Accessories & Supplies | Optoelectronic Displays |
Passive Components | Other Electronic Components | Electronic Signs |
EL Products | Walkie Talkie Accessories & Parts | Sim Cards & Accessories |
Mobile Phone Decorations | Mobile Phone Cases & Covers |
Mobile Phone Photography Accessories | Holders & Stands |
Mobile Phone Accessories | Mobile Phone Protective Film
"""
    ),
    "saved_miscellaneous": _names(
        """
Canned Food1 | Grocery | Frozen Products | Food&Fresh（Spe） |
Vegetables and Greens | Meat | Fruits and Berries | Bread and Pastries |
Canned Food | Sausages | Alcoholic Beverages | Fish and Sea Food |
Nut & Kernel1 | Dried Fruit1 | Grain Products1 | Coffee1 | Milk and Eggs |
Ready Meal | Water/ Juices/ Drinks | Dried Goods / Local Specialties |
Coffee | Dried Goods / Local Specialties1 | Cheese | Dried Fruit |
Grain Products | Nut & Kernel | Tea | Paper & Printable Media |
Drafting Supplies | Books（old） | Maps & Atlases | Journal |
Periodical & Magazines | Mailing & Shipping | Filing Products |
Desk Accessories & Organizer | Art Supplies | Presentation Supplies |
Accounting Supplies | Pens, Pencils & Writing Supplies | Printing Products |
Stationery Sticker | Notebooks & Writing Pads | School Supplies |
Tapes, Adhesives & Fasteners | Educational Equipment & Supplies |
Office Binding Supplies | Art Tool Kits | AE test01 | ae test | pv merge test |
post cat q(test) | AE test06 | test-long | postcat multilang |
Test zy leaf 3 | test zy leaf 3 | testqiaoyu | test zy leaf 04 |
test zy non leaf1 | P merge test | Travel Vocation | Hotel Booking |
Flight Booking | Mobile Phone Recharge | Offline Activity For New Seller |
Topup Commission Category | Mobile Phone Recharge1111 | Giftcard |
Prepaid Digital Codes | Giftcard Commission Category | Coupons | Event Ticket |
Woodworking Machinery & Accessories | Agricultural Machinery & Supplies |
Commercial Equipment | Packaging, Printing & Supporting Equipment |
Fabric & Textile Raw Material | Metals & Alloys |
Industrial and Commercial Cleaning Equipment | Industry Machinery & Equipment |
Industrial Automation Control & Accessories | Rubbers & Plastics |
Air Compressors | Pneumatics & Hydraulics | Chemicals | Blowers |
Industrial Fans & Exhaust Equipment | Food Machine and Supporting Equipment |
Construction Machinery & Accessories |
Metal Processing Equipment & Accessories |
Handling, Warehousing & Transportation Equipment | test zy sop leaf1 |
Mobilephone & Plug Type Test Only | Plug Type Test Only |
Home Textile Size Chart Test Only | Shoesize Chart Testing Only |
Lottery Tickets（Without VAT） | underwear size chart test only | Event Ticket |
Giftcard1 | Test for Sales Attribute Descriptions |
women's sports clothing size chart test only |
jewelry size chart test only | men's sports clothing size chart test only |
men's underwear size chart test only | men's formal wear size chart test only |
Down payment/ Purchasing Agent | Other | Upcoming Products | Giveaways |
Custom-made Charge | Additional Pay on Your Order | Checkout Link |
Prepaid Digital Codes | Giftcard | Coupons |
children clothing size chart test only | Test | Beginner for XinXiaoYuan |
children shoes size chart test only | Mobile Phone Recharge |
Overseas Warehouse | Platform Coupon | Wallpaper Sample | Mini App |
Men's Clothing Size Chart Testing Only | Custom-made-Charge |
Size chart testing only | Books | Music | CDs & Vinyl Records |
Cultural Derivatives(Office Supplies)
"""
    ),
}

SAVED_OFFER_PROVIDER_SUBCATEGORY_NAMES = tuple(
    dict.fromkeys(
        name
        for names in SAVED_OFFER_SUBCATEGORIES_BY_FILTER.values()
        for name in names
    )
)