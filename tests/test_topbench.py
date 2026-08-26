from benchmark_topbench import parse_topbench_ini, parse_topbench_metrics


def test_parse_topbench_component_metrics():
    screen = """
    Score:                  4
    Memory:                 3774 \u00b5s
    Effective addressing:   1935 us
    Opcode exercise:        1753 \u03bcs
    Vid adapter speed:      2652 us
    3DGame opcode exercise: 1851 \u00b5s
    """

    assert parse_topbench_metrics(screen) == {
        'score': 4,
        'memory_us': 3774,
        'effective_addressing_us': 1935,
        'opcode_us': 1753,
        'video_adapter_us': 2652,
        'three_d_game_us': 1851,
    }


def test_parse_topbench_metrics_allows_incomplete_live_screen():
    assert parse_topbench_metrics(
        "Iteration #0001 This system's TOPBENCH Score: 4\n") == {}


def test_parse_topbench_metrics_ignores_unrelated_text():
    assert parse_topbench_metrics(
        'TOPBENCH Initializing...\nScore: not ready\n') == {}


def test_parse_topbench_stub_ini():
    assert parse_topbench_ini("""
    [UID123]
    MemoryTest=2950
    OpcodeTest=1662
    VidramTest=1233
    MemEATest=1233
    3DGameTest=1662
    Score=6
    Machine=unknown
    """) == {
        'score': 6,
        'memory_us': 2950,
        'opcode_us': 1662,
        'video_adapter_us': 1233,
        'effective_addressing_us': 1233,
        'three_d_game_us': 1662,
    }
