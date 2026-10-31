"""An exhaustive field registry, shared by HTTP/MCP and independent of taxonomy UI lists."""
import asyncio
import json
import httpx
import pytest
import db
from config import settings
from src.spec.discovery import discover_specs, spec_values
from src.spec.vendor_index import recompute_vendor_stats
from src.search.filter_plan import FilterPlanError


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "discovery.db")
    db.init_db()
    conn = db.get_db_connection()
    for pid, title, category in [(1, "Microcontroller MCU", "microcontrollers"),
                                  (2, "Microcontroller MCU", "microcontrollers"),
                                  (3, "DC Motor", "motor")]:
        conn.execute("INSERT INTO products(id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical) VALUES (?,'robu','Robu',?,?,?,10,1,'https://example.invalid',?)",
                     (pid, str(pid), title, title.lower(), category))
    data = [(1, f"custom_field_{i:03}", "7", 7, "float", None, f"custom_field_{i:03}") for i in range(160)]
    data += [(1, "adc_bits", "12", 12, "float", None, "adc_bits"),
             (2, "adc_bits", "16", 16, "float", None, "adc_bits"),
             (3, "shaft_diameter_mm", "6", 6, "float", "mm", "shaft_diameter_mm")]
    conn.executemany("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(?,?,?,?,?,?,?)", data)
    recompute_vendor_stats(conn)
    yield conn
    conn.close()


def test_every_rare_field_can_be_discovered_beyond_100(catalog):
    keys, cursor = [], None
    while True:
        page = discover_specs(catalog, category="microcontrollers", limit=17, cursor=cursor)
        keys += [field["key"] for field in page["fields"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(keys) == len(set(keys)) == 161
    assert "custom_field_159" in keys
    assert "shaft_diameter_mm" not in keys


def test_field_search_and_cursor_scope(catalog):
    page = discover_specs(catalog, category="microcontrollers", search="adc")
    assert [r["key"] for r in page["fields"]] == ["adc_bits"]
    assert page["fields"][0]["filter_parameter"] == "vendor_filters"
    assert "gte" in page["fields"][0]["operators"]
    cursor = discover_specs(catalog, limit=1)["next_cursor"]
    with pytest.raises(FilterPlanError):
        discover_specs(catalog, category="motor", limit=1, cursor=cursor)


def test_value_pages_are_exhaustive_and_coverage_is_usable(catalog):
    first = spec_values(catalog, "adc_bits", category="microcontrollers", limit=1)
    second = spec_values(catalog, "adc_bits", category="microcontrollers", limit=1, cursor=first["next_cursor"])
    assert first["coverage_count"] == 2
    assert first["missing_or_unusable"] == 0
    assert len(first["values"]) == len(second["values"]) == 1
    assert first["values"] != second["values"]
    assert second["next_cursor"] is None


def test_http_and_mcp_share_the_same_registry(catalog, monkeypatch):
    import main
    from mcp_server import server
    monkeypatch.setattr(server, "_description_schema_ready", True)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.web_app),base_url="http://test") as client:
            return await client.get("/api/specs", params={"category":"microcontrollers","q":"custom field 159"})
    response = asyncio.run(run())
    result = server.discover_specs(category="microcontrollers", search="custom field 159")
    assert response.status_code == 200 and result.ok
    assert response.json() == result.data
    assert result.data["fields"][0]["key"] == "custom_field_159"


def test_discovered_field_is_directly_filterable_via_mcp(catalog, monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server, "_description_schema_ready", True)
    field = server.discover_specs(category="microcontrollers", search="adc").data["fields"][0]
    result = server.search_components(category="microcontrollers", refresh=False,
                                      **{field["filter_parameter"]:{field["key"]:12}})
    assert result.ok and result.total_matches == 1
    assert result.data["results"][0]["product_id"] == 1


@pytest.mark.parametrize("args", [{"category":"nonsense"}, {"tier":"nonsense"}, {"limit":101}, {"cursor":"bogus"}])
def test_invalid_discovery_arguments_are_explicit_errors(catalog, args):
    with pytest.raises(FilterPlanError):
        discover_specs(catalog, **args)


@pytest.mark.parametrize("query,category,field,value,expected", [
    ("microcontroller 12 bit ADC", "microcontrollers", "adc_bits", 12, 1),
    ("microcontroller 12-bit ADC", "microcontrollers", "adc_bits", 12, 1),
    ("microcontroller ADC bits at least 12", "microcontrollers", "adc_bits", {"gte":12}, 2),
    ("motor shaft diameter 6mm", "motor", "shaft_diameter_mm", 6, 1),
    ("microcontroller custom field 159 = 7", "microcontrollers", "custom_field_159", 7, 1),
])
def test_named_specs_use_live_fields_rather_than_a_fixed_property_list(catalog, monkeypatch, query, category, field, value, expected):
    from mcp_server import server
    monkeypatch.setattr(server, "_description_schema_ready", True)
    text = server.search_components(query=query, category=category, refresh=False)
    structured = server.search_components(category=category, vendor_filters={field:value}, refresh=False)
    assert text.ok and structured.ok
    assert text.total_matches == structured.total_matches == expected
    assert text.data["query_contract"]["engineering_constraints"][0]["key"] == field


def test_named_parametric_category_does_not_require_its_literal_title_spelling(catalog, monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.execute("UPDATE products SET title='STM32F103 MCU', normalized_title='stm32f103 mcu' WHERE id=1")
    catalog.commit()
    found=server.search_components(query='microcontroller 12 bit ADC',refresh=False)
    assert found.ok and found.total_matches==1
    assert found.data['query_contract']['category_terms_interpreted']==['microcontroller']
    protected=server.search_components(query='ESP32 microcontroller 12 bit ADC',refresh=False)
    assert protected.ok and protected.total_matches==0


def test_registered_mcp_discovery_tools_validate_and_serialize(catalog, monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    result=asyncio.run(server.mcp.call_tool('discover_specs',{'category':'microcontrollers','search':'adc'}))
    payload=result[1] if isinstance(result,tuple) else result
    assert payload['ok'] and payload['data']['fields'][0]['key']=='adc_bits'
    values=asyncio.run(server.mcp.call_tool('spec_values',{'key':'adc_bits','category':'microcontrollers'}))
    payload=values[1] if isinstance(values,tuple) else values
    assert payload['ok'] and payload['data']['coverage_count']==2
    invalid=asyncio.run(server.mcp.call_tool('discover_specs',{'limit':'ten'}))
    payload=invalid[1] if isinstance(invalid,tuple) else invalid
    assert not payload['ok'] and payload['error_code']=='invalid_arguments'


def test_http_vendor_filters_share_mcp_eligibility(catalog, monkeypatch):
    import main
    from mcp_server import server
    monkeypatch.setattr(main,'engine_instance',None)
    monkeypatch.setattr(server,'_description_schema_ready',True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.web_app),base_url='http://test') as client:
            return await client.get('/api/search',params={'category':'microcontrollers','vendor_filters':json.dumps({'adc_bits':12}),'refresh':'off'})
    response=asyncio.run(run())
    mcp=server.count_components(category='microcontrollers',vendor_filters={'adc_bits':12})
    assert response.status_code==200
    assert response.json()['total_results']==mcp.total_matches==1


def test_category_prefixed_raw_property_cannot_shadow_shared_property(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.execute("INSERT INTO products(id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical) VALUES (4,'robu','Robu','4','DC Motor','dc motor',10,1,'https://example.invalid','motor')")
    catalog.executemany("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(?,?,?,?,?,?,?)",[
        (3,'motor_shaft_diameter','6',6,'float','mm','motor_shaft_diameter'),
        (4,'shaft_diameter_mm','6',6,'float','mm','shaft_diameter_mm')])
    recompute_vendor_stats(catalog)
    result=server.search_components(query='motor shaft diameter 6mm',refresh=False)
    assert result.ok and result.total_matches==2
    assert result.data['query_contract']['engineering_constraints'][0]['key']=='shaft_diameter_mm'


def test_vendor_field_label_cannot_consume_a_required_model_identifier(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.execute("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(1,'esp32_voltage','5',5,'float','V','esp32_voltage')")
    recompute_vendor_stats(catalog)
    required_model=server.search_components(query='ESP32 voltage 5V',refresh=False)
    assert required_model.ok and required_model.total_matches==0
    explicit_field=server.search_components(vendor_filters={'esp32_voltage':5},refresh=False)
    assert explicit_field.ok and explicit_field.total_matches==1


def test_value_inspection_uses_the_same_global_alias_evidence_as_search(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.execute("INSERT INTO products(id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical) VALUES(4,'robu','Robu','4','DC Motor','dc motor',10,1,'https://example.invalid','motor')")
    catalog.execute("INSERT INTO product_specs(product_id,key,value,value_num,value_type) VALUES(3,'voltage','12',12,'float')")
    catalog.execute("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(4,'voltage_v','12',12,'float','V','voltage_v')")
    catalog.executemany("INSERT INTO spec_stats_global(key,value_type,applicable_count,value_counts) VALUES(?,'float',1,'{}')",[('voltage',),('voltage_v',)])
    catalog.execute("INSERT INTO spec_definitions(category,key,display_name,value_type,unit) VALUES('motor','voltage','Voltage','float','V')")
    recompute_vendor_stats(catalog)
    values=server.spec_values('voltage',category='motor',tier='promoted')
    count=server.count_components(category='motor',filters={'voltage':12})
    assert values.ok and count.ok
    assert values.data['coverage_count']==count.total_matches==2


def test_routed_and_explicit_vendor_constraints_are_both_required(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    result=server.count_components(category='microcontrollers',filters={'adc_bits':12},vendor_filters={'adc_bits':16})
    assert result.ok and result.total_matches==0
    compatible=server.count_components(category='microcontrollers',filters={'adc_bits':{'gte':12}},vendor_filters={'adc_bits':{'lte':16}})
    assert compatible.ok and compatible.total_matches==2


@pytest.mark.parametrize('clauses,expected',[
    ([{'in':[12,16]},{'in':[16,20]}],1),
    ([{'eq':12,'tolerance':0.1},{'gte':12}],1),
    ([{'gte':12},{'lt':16}],1),
])
def test_conjunction_operator_preserves_all_requirement_semantics(catalog,monkeypatch,clauses,expected):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    count=server.count_components(category='microcontrollers',vendor_filters={'adc_bits':{'all':clauses}})
    search=server.search_components(category='microcontrollers',vendor_filters={'adc_bits':{'all':clauses}},refresh=False)
    assert count.ok and search.ok and count.total_matches==search.total_matches==expected


@pytest.mark.parametrize('invalid',[[],None,True,[None],[True]])
def test_malformed_conjunctions_are_client_errors(catalog,monkeypatch,invalid):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    result=server.count_components(vendor_filters={'adc_bits':{'all':invalid}})
    assert not result.ok and result.error_code=='invalid_filter_value'


def test_deep_conjunction_is_a_client_error_instead_of_recursion_failure(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    value=12
    for _ in range(20):
        value={'all':[value]}
    result=server.count_components(vendor_filters={'adc_bits':value})
    assert not result.ok and result.error_code=='invalid_filter_value'


def test_stored_curated_field_without_ui_definition_roundtrips(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.execute("INSERT INTO product_specs(product_id,key,value,value_num,value_type) VALUES(1,'novel_measurement','7',7,'float')")
    catalog.execute("INSERT INTO spec_stats_global(key,value_type,applicable_count,value_counts) VALUES('novel_measurement','float',1,'{}')")
    catalog.commit()
    field=server.discover_specs(category='microcontrollers',search='novel').data['fields'][0]
    values=server.spec_values(field['key'],category='microcontrollers',tier=field['tier'])
    count=server.count_components(category='microcontrollers',**{field['filter_parameter']:{field['key']:7}})
    assert values.ok and count.ok
    assert values.data['coverage_count']==count.total_matches==1


def test_alias_value_page_does_not_validate_the_entire_vocabulary(catalog):
    for i in range(100):
        pid=100+i
        catalog.execute("INSERT INTO products(id,vendor,vendor_name,external_id,title,normalized_title,price,available,product_url,category_canonical) VALUES(?,'robu','Robu',?,'DC Motor','dc motor',10,1,'https://example.invalid','motor')",(pid,str(pid)))
        catalog.execute("INSERT INTO product_specs(product_id,key,value,value_num,value_type) VALUES(?,'voltage',?,?,'float')",(pid,str(i),i))
        catalog.execute("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(?,'voltage_v',?,?,'float','V','voltage_v')",(pid,str(i),i))
    catalog.executemany("INSERT INTO spec_stats_global(key,value_type,applicable_count,value_counts) VALUES(?,'float',100,'{}')",[('voltage',),('voltage_v',)])
    recompute_vendor_stats(catalog)
    statements=[]
    catalog.set_trace_callback(statements.append)
    try:
        page=spec_values(catalog,'voltage',category='motor',limit=1)
    finally:
        catalog.set_trace_callback(None)
    assert len(page['values'])==1 and page['values'][0]['count']==1
    assert page['has_more'] and page['next_cursor']
    assert page['total_values'] is None and page['total_value_candidates']==100
    assert len([s for s in statements if s.lstrip().upper().startswith(('SELECT','WITH'))])<30
    next_page=spec_values(catalog,'voltage',category='motor',limit=1,cursor=page['next_cursor'])
    assert next_page['values']!=page['values']


def test_negative_numeric_values_keep_their_sign_and_roundtrip_counts(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.executemany("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(?,'operating_temperature_min',?,?,'float','C','operating_temperature_min')",[(1,'-40',-40),(2,'40',40)])
    recompute_vendor_stats(catalog)
    values=server.spec_values('operating_temperature_min',category='microcontrollers')
    assert values.ok
    assert {item['value']:item['count'] for item in values.data['values']}=={'-40.0':1,'40.0':1}
    for item in values.data['values']:
        count=server.count_components(category='microcontrollers',vendor_filters={'operating_temperature_min':item['value']})
        assert count.ok and count.total_matches==item['count']


def test_vendor_field_types_and_ranges_follow_the_selected_category(catalog,monkeypatch):
    from mcp_server import server
    monkeypatch.setattr(server,'_description_schema_ready',True)
    catalog.executemany("INSERT INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key) VALUES(?,'calibration_setting',?,? ,?,NULL,'calibration_setting')",[
        (1,'7',7,'float'),(3,'factory',None,'string')])
    recompute_vendor_stats(catalog)
    motor_field=server.discover_specs(category='motor',search='calibration').data['fields'][0]
    assert motor_field['type']=='text' and motor_field['range'] is None
    counted=server.count_components(category='motor',vendor_filters={'calibration_setting':'factory'})
    assert counted.ok and counted.total_matches==1
    numeric=server.discover_specs(category='microcontrollers',search='calibration').data['fields'][0]
    assert numeric['type']=='number' and numeric['range']=={'min':7.0,'max':7.0}
