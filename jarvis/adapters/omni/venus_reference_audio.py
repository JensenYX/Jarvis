def reset_reference_audio_normalization(processor):
    processor.audio_processor.set_spac_log_norm(dynamic_range_db=8.0)
