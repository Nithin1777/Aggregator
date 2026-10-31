"""New and existing scraped evidence must become discoverable without manual promotion."""
import json
import pytest
import db
from config import settings
from models import UnifiedProduct
from src.spec.discovery import discover_specs
from src.search.service import CatalogSearch
from src.spec.enrichment import ensure_enrichment_schema
from scripts.enrich_catalog import backfill_catalog


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'db_path',tmp_path/'enrichment.db')
    db.init_db()
    conn=db.get_db_connection()
    yield conn
    conn.close()


def product(**kwargs):
    data=dict(vendor='flyrobo',vendor_name='Flyrobo',external_id='1',title='12V 100RPM DC Motor',
              normalized_title='12v 100rpm dc motor',price=100,available=True,product_url='https://example.invalid')
    data.update(kwargs)
    return UnifiedProduct(**data)


def test_new_ingestion_publishes_title_specs_and_metadata(catalog):
    db.upsert_products([product()])
    row=catalog.execute('SELECT id,category_canonical FROM products').fetchone()
    assert row['category_canonical']=='motor'
    fields=discover_specs(catalog,category='motor')
    assert {'rpm','voltage'} <= {f['key'] for f in fields['fields']}
    search=CatalogSearch(catalog)
    assert search.count(search.prepare('12V motor 100 rpm'))==1


def test_ingestion_preserves_source_html_and_indexes_unknown_vendor_fields(catalog):
    html='<table><tr><td>Uncommon Optical Mode</td><td>Infrared</td></tr></table>'
    listing=product(title='Optical Sensor Module',description_html=html)
    db.upsert_products([listing])
    assert 'description_html' not in listing.model_dump()
    assert catalog.execute('SELECT description FROM product_descriptions').fetchone()[0]==html
    field=discover_specs(catalog,category='sensors',search='uncommon optical')['fields'][0]
    assert field['key']=='uncommon_optical_mode' and field['filter_parameter']=='vendor_filters'
    search=CatalogSearch(catalog)
    assert search.count(search.prepare(category='sensors',vendor_filters={'uncommon_optical_mode':'infrared'}))==1


def test_attribute_import_is_immediately_queryable(catalog):
    db.upsert_products([product()])
    pid=catalog.execute('SELECT id FROM products').fetchone()[0]
    db.replace_product_attributes(pid,[{'key':'shaft_diameter_mm','value':'6','value_num':6,'unit':'mm'}])
    fields=discover_specs(catalog,category='motor',search='shaft')
    assert fields['fields'][0]['key']=='shaft_diameter_mm'
    search=CatalogSearch(catalog)
    assert search.count(search.prepare(category='motor',vendor_filters={'shaft_diameter_mm':6}))==1


def test_price_only_upsert_retains_description_and_table_evidence(catalog):
    db.upsert_products([product(description_html='<p>Shaft Diameter: 6mm</p>')])
    before=catalog.execute('SELECT description FROM product_descriptions').fetchone()[0]
    db.upsert_products([product(price=250)])
    assert catalog.execute('SELECT description FROM product_descriptions').fetchone()[0]==before
    assert catalog.execute("SELECT COUNT(*) FROM product_attributes WHERE source='description_text'").fetchone()[0]>0


def test_existing_catalog_backfill_is_idempotent_and_preserves_source_rows(catalog):
    catalog.execute("INSERT INTO products(id,vendor,vendor_name,external_id,title,normalized_title,product_url) VALUES(1,'flyrobo','Flyrobo','1','12V 100RPM DC Motor','12v 100rpm dc motor','https://example.invalid')")
    catalog.execute("INSERT INTO product_descriptions(product_id,description) VALUES(1,'<p>Shaft Diameter: 6mm</p>')")
    catalog.commit()
    before=catalog.total_changes
    preview=backfill_catalog(catalog)
    assert preview['eligible_products']==1 and catalog.total_changes==before
    first=backfill_catalog(catalog,apply=True,batch_size=1)
    second=backfill_catalog(catalog,apply=True,batch_size=1)
    assert first['changed']==1 and second['changed']==0
    assert catalog.execute('SELECT COUNT(*) FROM products').fetchone()[0]==1
    assert 'Shaft Diameter' in catalog.execute('SELECT description FROM product_descriptions').fetchone()[0]
    assert CatalogSearch(catalog).count(CatalogSearch(catalog).prepare(category='motor',vendor_filters={'shaft_diameter_mm':6}))==1


def test_description_table_wins_over_conflicting_title_heuristic(catalog):
    db.upsert_products([product(description_html='<table><tr><td>Operating Voltage</td><td>24V</td></tr></table>')])
    value=catalog.execute("SELECT value_num FROM product_vendor_specs WHERE key='voltage'").fetchone()[0]
    assert value==24


def test_batch_failure_rolls_back_listing_and_derived_evidence(catalog, monkeypatch):
    import src.spec.enrichment as enrichment
    original=enrichment.enrich_listing
    def fail(conn,pid,**kwargs):
        result=original(conn,pid,**kwargs)
        raise RuntimeError('injected enrichment failure')
    monkeypatch.setattr(enrichment,'enrich_listing',fail)
    with pytest.raises(RuntimeError,match='injected'):
        db.upsert_products([product()])
    assert catalog.execute('SELECT COUNT(*) FROM products').fetchone()[0]==0
    assert catalog.execute('SELECT COUNT(*) FROM product_vendor_specs').fetchone()[0]==0


def test_stored_ranges_get_discoverable_numeric_endpoints_without_losing_source(catalog):
    db.upsert_products([product(description_html='<table><tr><td>Input Voltage Range</td><td>3-6V</td></tr></table>')])
    rows={r['key']:dict(r) for r in catalog.execute('SELECT * FROM product_vendor_specs')}
    assert rows['input_voltage_range']['value']=='3-6v'
    assert rows['input_voltage_range_min']['value_num']==3
    assert rows['input_voltage_range_max']['value_num']==6
    assert rows['input_voltage_range_min']['source']=='vendor_interval'
    assert CatalogSearch(catalog).count(CatalogSearch(catalog).prepare(vendor_filters={'input_voltage_range_min':{'lte':3},'input_voltage_range_max':{'gte':5}}))==1


def test_symmetric_coefficient_bounds_are_queryable_without_coercing_dielectric_names(catalog):
    db.upsert_products([product(title='10K ohm Resistor',description_html='<p>Temperature Coefficient: ±100ppm/℃</p>')])
    rows={r['key']:dict(r) for r in catalog.execute('SELECT * FROM product_vendor_specs')}
    assert rows['temperature_coefficient_min']['value_num']==-100
    assert rows['temperature_coefficient_max']['value_num']==100
    assert rows['temperature_coefficient']['value_num'] is None
    assert CatalogSearch(catalog).count(CatalogSearch(catalog).prepare(vendor_filters={'temperature_coefficient_max':{'lte':'100ppm/C'}}))==1


def test_new_unit_dimension_is_normalized_for_arbitrary_vendor_property(catalog):
    db.upsert_products([product(title='Optical Sensor Module',description_html='<p>Response Time: 37ms</p>')])
    row=catalog.execute("SELECT value_num,unit FROM product_vendor_specs WHERE key='response_time'").fetchone()
    assert tuple(row)==(0.037,'s')
    assert CatalogSearch(catalog).count(CatalogSearch(catalog).prepare(vendor_filters={'response_time':{'lte':'40ms'}}))==1
    assert CatalogSearch(catalog).count(CatalogSearch(catalog).prepare('sensor response time under 40ms'))==1


@pytest.mark.parametrize('value,expected', [('3V-6000mV',(3,6)),('-40°C to +85°C',(-40,85))])
def test_intervals_support_units_on_both_endpoints(value,expected):
    from src.spec.vendor_index import _numeric_interval
    key='input_voltage' if 'V' in value else 'operating_temperature'
    interval=_numeric_interval(key,value,None)
    assert interval[:2]==expected


def test_a_scraped_first_number_does_not_turn_a_range_into_a_scalar():
    from src.spec.vendor_index import normalize_vendor_value
    assert normalize_vendor_value('input_voltage','3-6V',3,'V')[1] is None
    assert normalize_vendor_value('voltage','3A',3,'A')[1] is None


def test_full_index_rebuild_keeps_the_same_evidence_priority(catalog):
    from src.spec.vendor_index import rebuild_vendor_specs
    db.upsert_products([product(description_html='<table><tr><td>Operating Voltage</td><td>24V</td></tr></table>')])
    rebuild_vendor_specs(catalog)
    assert catalog.execute("SELECT value_num FROM product_vendor_specs WHERE key='voltage'").fetchone()[0]==24


def test_ingestion_does_not_silently_drop_attributes_after_eighty(catalog):
    html='<table>'+''.join(f'<tr><td>Custom field {i:03}</td><td>{i}</td></tr>' for i in range(105))+'</table>'
    db.upsert_products([product(description_html=html)])
    assert catalog.execute("SELECT COUNT(*) FROM product_attributes WHERE key LIKE 'custom_field_%'").fetchone()[0]==105
    found=discover_specs(catalog,search='custom field 104')
    assert found['fields'][0]['key']=='custom_field_104'


def test_flyrobo_product_page_retains_description_for_the_shared_pipeline(catalog):
    from adapters.flyrobo import FlyroboAdapter
    node={'@type':'Product','name':'12V DC Motor','model':'1',
          'offers':{'price':'100','availability':'https://schema.org/InStock'}}
    html='<script type="application/ld+json">'+json.dumps(node)+'</script><div id="tab-description"><p>Shaft Diameter: 6mm</p></div>'
    listing=FlyroboAdapter()._parse_product_page(html,'https://www.flyrobo.in/motor','motor')
    assert listing and 'Shaft Diameter' in listing.description_html
    db.upsert_products([listing])
    assert catalog.execute("SELECT COUNT(*) FROM product_vendor_specs WHERE key='shaft_diameter_mm'").fetchone()[0]==1


def test_targeted_missing_description_backfill_does_not_refresh_prices(catalog,monkeypatch):
    import asyncio
    from adapters.flyrobo import FlyroboAdapter
    from scripts.backfill_descriptions import _flyrobo
    db.upsert_products([product(price=321)])
    calls=[]
    async def fetch(self,url,**kwargs):
        calls.append(url)
        return '<div id="tab-description"><p>Shaft Diameter: 6mm</p></div>'
    monkeypatch.setattr(FlyroboAdapter,'_fetch_text',fetch)
    result=asyncio.run(_flyrobo(catalog,1,0,False,False))
    assert result['saved']==1 and result['failed']==0
    assert catalog.execute('SELECT price FROM products').fetchone()[0]==321
    assert catalog.execute("SELECT COUNT(*) FROM product_vendor_specs WHERE key='shaft_diameter_mm'").fetchone()[0]==1
    repeat=asyncio.run(_flyrobo(catalog,1,0,False,False))
    assert repeat['skipped']==1 and len(calls)==1
