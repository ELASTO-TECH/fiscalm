<?php
/** Remove saved credentials when WordPress explicitly uninstalls the plugin. */
defined( 'WP_UNINSTALL_PLUGIN' ) || exit;
delete_option( 'fiscalm_settings' );
if ( function_exists( 'as_unschedule_all_actions' ) ) {
	as_unschedule_all_actions( 'fiscalm_job', null, 'fiscalm' );
	as_unschedule_all_actions( 'fiscalm_awb_watch', null, 'fiscalm' );
}
