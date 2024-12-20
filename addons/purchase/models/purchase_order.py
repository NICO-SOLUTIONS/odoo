# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
from collections import defaultdict
from datetime import datetime
from dateutil.relativedelta import relativedelta
from pytz import timezone

from markupsafe import escape, Markup
from werkzeug.urls import url_encode

from odoo import api, Command, fields, models, _
from odoo.osv import expression
from odoo.tools import format_amount, format_date, formatLang, groupby, OrderedSet, SQL
from odoo.tools.float_utils import float_is_zero, float_repr
from odoo.exceptions import UserError, ValidationError


class PurchaseOrder(models.Model):
    _name = 'purchase.order'
    _inherit = ['portal.mixin', 'product.catalog.mixin', 'mail.thread', 'mail.activity.mixin']
    _description = "Purchase Order"
    _rec_names_search = ['name', 'partner_ref']
    _order = 'priority desc, id desc'

    @api.depends('order_line.price_subtotal', 'company_id')
    def _amount_all(self):
        AccountTax = self.env['account.tax']
        for order in self:
            order_lines = order.order_line.filtered(lambda x: not x.display_type)
            base_lines = [line._prepare_base_line_for_taxes_computation() for line in order_lines]
            AccountTax._add_tax_details_in_base_lines(base_lines, order.company_id)
            AccountTax._round_base_lines_tax_details(base_lines, order.company_id)
            tax_totals = AccountTax._get_tax_totals_summary(
                base_lines=base_lines,
                currency=order.currency_id or order.company_id.currency_id,
                company=order.company_id,
            )
            order.amount_untaxed = tax_totals['base_amount_currency']
            order.amount_tax = tax_totals['tax_amount_currency']
            order.amount_total = tax_totals['total_amount_currency']
            order.amount_total_cc = tax_totals['total_amount']

    @api.depends('state', 'order_line.qty_to_invoice')
    def _get_invoiced(self):
        precision = self.env['decimal.precision'].precision_get('Product Unit')
        for order in self:
            if order.state != 'purchase':
                order.invoice_status = 'no'
                continue

            if any(
                not float_is_zero(line.qty_to_invoice, precision_digits=precision)
                for line in order.order_line.filtered(lambda l: not l.display_type)
            ):
                order.invoice_status = 'to invoice'
            elif (
                all(
                    float_is_zero(line.qty_to_invoice, precision_digits=precision)
                    for line in order.order_line.filtered(lambda l: not l.display_type)
                )
                and order.invoice_ids
            ):
                order.invoice_status = 'invoiced'
            else:
                order.invoice_status = 'no'

    @api.depends('order_line.invoice_lines.move_id')
    def _compute_invoice(self):
        for order in self:
            invoices = order.mapped('order_line.invoice_lines.move_id')
            order.invoice_ids = invoices
            order.invoice_count = len(invoices)

    name = fields.Char('Order Reference', required=True, index='trigram', copy=False, default='New')
    priority = fields.Selection(
        [('0', 'Normal'), ('1', 'Urgent')], 'Priority', default='0', index=True)
    origin = fields.Char('Source', copy=False,
        help="Reference of the document that generated this purchase order "
             "request (e.g. a sales order)")
    partner_ref = fields.Char('Vendor Reference', copy=False,
        help="Reference of the sales order or bid sent by the vendor. "
             "It's used to do the matching when you receive the "
             "products as this reference is usually written on the "
             "delivery order sent by your vendor.")
    date_order = fields.Datetime('Order Deadline', required=True, index=True, copy=False, default=fields.Datetime.now,
        help="Depicts the date within which the Quotation should be confirmed and converted into a purchase order.")
    date_approve = fields.Datetime('Confirmation Date', readonly=True, index=True, copy=False)
    partner_id = fields.Many2one('res.partner', string='Vendor', required=True, change_default=True, tracking=True, check_company=True, help="You can find a vendor by its Name, TIN, Email or Internal Reference.")
    dest_address_id = fields.Many2one('res.partner', check_company=True, string='Dropship Address',
        help="Put an address if you want to deliver directly from the vendor to the customer. "
             "Otherwise, keep empty to deliver to your own company.")
    currency_id = fields.Many2one('res.currency', 'Currency', required=True,
        default=lambda self: self.env.company.currency_id.id)
    state = fields.Selection([
        ('draft', 'RFQ'),
        ('sent', 'RFQ Sent'),
        ('to approve', 'To Approve'),
        ('purchase', 'Purchase Order'),
        ('cancel', 'Cancelled')
    ], string='Status', readonly=True, index=True, copy=False, default='draft', tracking=True)
    locked = fields.Boolean(
        help="Locked Purchase Orders cannot be modified.",
        default=False,
        copy=False,
        tracking=True)
    lock_confirmed_po = fields.Selection(related="company_id.po_lock")
    order_line = fields.One2many('purchase.order.line', 'order_id', string='Order Lines', copy=True)
    acknowledged = fields.Boolean(
        'Acknowledged', copy=False, tracking=True,
        help="It indicates that the vendor has acknowledged the receipt of the purchase order.")
    notes = fields.Html('Terms and Conditions')

    partner_bill_count = fields.Integer(related='partner_id.supplier_invoice_count')
    invoice_count = fields.Integer(compute="_compute_invoice", string='Bill Count', copy=False, default=0, store=True)
    invoice_ids = fields.Many2many('account.move', compute="_compute_invoice", string='Bills', copy=False, store=True)
    invoice_status = fields.Selection([
        ('no', 'Nothing to Bill'),
        ('to invoice', 'Waiting Bills'),
        ('invoiced', 'Fully Billed'),
    ], string='Billing Status', compute='_get_invoiced', store=True, readonly=True, copy=False, default='no')
    date_planned = fields.Datetime(
        string='Expected Arrival', index=True, copy=False, compute='_compute_date_planned', store=True, readonly=False,
        help="Delivery date promised by vendor. This date is used to determine expected arrival of products.")
    date_calendar_start = fields.Datetime(compute='_compute_date_calendar_start', readonly=True, store=True)

    amount_untaxed = fields.Monetary(string='Untaxed Amount', store=True, readonly=True, compute='_amount_all', tracking=True)
    tax_totals = fields.Binary(compute='_compute_tax_totals', exportable=False)
    amount_tax = fields.Monetary(string='Taxes', store=True, readonly=True, compute='_amount_all')
    amount_total = fields.Monetary(string='Total', store=True, readonly=True, compute='_amount_all')
    amount_total_cc = fields.Monetary(string="Total in currency", store=True, readonly=True, compute="_amount_all", currency_field="company_currency_id")

    fiscal_position_id = fields.Many2one('account.fiscal.position', string='Fiscal Position', domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    tax_country_id = fields.Many2one(
        comodel_name='res.country',
        compute='_compute_tax_country_id',
        # Avoid access error on fiscal position, when reading a purchase order with company != user.company_ids
        compute_sudo=True,
        help="Technical field to filter the available taxes depending on the fiscal country and fiscal position.")
    tax_calculation_rounding_method = fields.Selection(
        related='company_id.tax_calculation_rounding_method',
        string='Tax calculation rounding method', readonly=True)
    payment_term_id = fields.Many2one('account.payment.term', 'Payment Terms', domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    incoterm_id = fields.Many2one('account.incoterms', 'Incoterm', help="International Commercial Terms are a series of predefined commercial terms used in international transactions.")

    product_id = fields.Many2one('product.product', related='order_line.product_id', string='Product')
    user_id = fields.Many2one(
        'res.users', string='Buyer', index=True, tracking=True,
        default=lambda self: self.env.user, check_company=True)
    company_id = fields.Many2one('res.company', 'Company', required=True, index=True, default=lambda self: self.env.company.id)
    company_currency_id = fields.Many2one(related="company_id.currency_id", string="Company Currency")
    country_code = fields.Char(related='company_id.account_fiscal_country_id.code', string="Country code")
    company_price_include = fields.Selection(related='company_id.account_price_include')
    currency_rate = fields.Float(
        string="Currency Rate",
        compute='_compute_currency_rate',
        digits=0,
        store=True,
        precompute=True,
    )

    receipt_reminder_email = fields.Boolean('Receipt Reminder Email', compute='_compute_receipt_reminder_email', store=True, readonly=False)
    reminder_date_before_receipt = fields.Integer('Days Before Receipt', compute='_compute_receipt_reminder_email', store=True, readonly=False)

    is_late = fields.Boolean('Is Late', store=False, search='_search_is_late')
    show_comparison = fields.Boolean('Show Comparison', compute='_compute_show_comparison')

    purchase_warning_text = fields.Text(
        "Purchase Warning",
        help="Internal warning for the partner or the products as set by the user.",
        compute='_compute_purchase_warning_text')

    @api.constrains('company_id', 'order_line')
    def _check_order_line_company_id(self):
        for order in self:
            invalid_companies = order.order_line.product_id.company_id.filtered(
                lambda c: order.company_id not in c._accessible_branches()
            )
            if invalid_companies:
                bad_products = order.order_line.product_id.filtered(
                    lambda p: p.company_id and p.company_id in invalid_companies
                )
                raise ValidationError(_(
                    "Your quotation contains products from company %(product_company)s whereas your quotation belongs to company %(quote_company)s. \n Please change the company of your quotation or remove the products from other companies (%(bad_products)s).",
                    product_company=', '.join(invalid_companies.sudo().mapped('display_name')),
                    quote_company=order.company_id.display_name,
                    bad_products=', '.join(bad_products.mapped('display_name')),
                ))

    def _compute_access_url(self):
        super(PurchaseOrder, self)._compute_access_url()
        for order in self:
            order.access_url = '/my/purchase/%s' % (order.id)

    @api.depends('state', 'date_order', 'date_approve')
    def _compute_date_calendar_start(self):
        for order in self:
            order.date_calendar_start = order.date_approve if (order.state == 'purchase') else order.date_order

    @api.depends('currency_id', 'date_order', 'company_id')
    def _compute_currency_rate(self):
        for order in self:
            order.currency_rate = self.env['res.currency']._get_conversion_rate(
                from_currency=order.company_id.currency_id,
                to_currency=order.currency_id,
                company=order.company_id,
                date=(order.date_order or fields.Datetime.now()).date(),
            )

    @api.depends('amount_total', 'currency_rate')
    def _compute_amount_total_cc(self):
        for order in self:
            order.amount_total_cc = order.amount_total / order.currency_rate

    @api.depends('order_line.date_planned')
    def _compute_date_planned(self):
        """ date_planned = the earliest date_planned across all order lines. """
        for order in self:
            dates_list = order.order_line.filtered(lambda x: not x.display_type and x.date_planned).mapped('date_planned')
            if dates_list:
                order.date_planned = min(dates_list)
            else:
                order.date_planned = False

    @api.depends('name', 'partner_ref', 'amount_total', 'currency_id')
    @api.depends_context('show_total_amount')
    def _compute_display_name(self):
        for po in self:
            name = po.name
            if po.partner_ref:
                name += ' (' + po.partner_ref + ')'
            if self.env.context.get('show_total_amount') and po.amount_total:
                name += ': ' + formatLang(self.env, po.amount_total, currency_obj=po.currency_id)
            po.display_name = name

    @api.depends('company_id', 'partner_id', 'partner_id.reminder_date_before_receipt')
    def _compute_receipt_reminder_email(self):
        for order in self:
            order.receipt_reminder_email = order.partner_id.with_company(order.company_id).receipt_reminder_email
            order.reminder_date_before_receipt = order.partner_id.with_company(order.company_id).reminder_date_before_receipt

    @api.depends_context('lang')
    @api.depends('order_line.price_subtotal', 'currency_id', 'company_id')
    def _compute_tax_totals(self):
        AccountTax = self.env['account.tax']
        for order in self:
            if not order.company_id:
                order.tax_totals = False
                continue
            order_lines = order.order_line.filtered(lambda x: not x.display_type)
            base_lines = [line._prepare_base_line_for_taxes_computation() for line in order_lines]
            AccountTax._add_tax_details_in_base_lines(base_lines, order.company_id)
            AccountTax._round_base_lines_tax_details(base_lines, order.company_id)
            order.tax_totals = AccountTax._get_tax_totals_summary(
                base_lines=base_lines,
                currency=order.currency_id or order.company_id.currency_id,
                company=order.company_id,
            )
            if order.currency_id != order.company_currency_id:
                order.tax_totals['amount_total_cc'] = f"({formatLang(self.env, order.amount_total_cc, currency_obj=self.company_currency_id)})"

    @api.depends('company_id.account_fiscal_country_id', 'fiscal_position_id.country_id', 'fiscal_position_id.foreign_vat')
    def _compute_tax_country_id(self):
        for record in self:
            if record.fiscal_position_id.foreign_vat:
                record.tax_country_id = record.fiscal_position_id.country_id
            else:
                record.tax_country_id = record.company_id.account_fiscal_country_id

    @api.depends('order_line', 'order_line.product_id')
    def _compute_show_comparison(self):
        line_groupby_product = self.env['purchase.order.line']._read_group(
            [('product_id', 'in', self.order_line.product_id.ids), ('state', '=', 'purchase')],
            ['product_id'],
            ['order_id:array_agg']
        )

        order_by_product = {p: set(o_ids) for p, o_ids in line_groupby_product}
        for record in self:
            record.show_comparison = any(set(record.ids) != order_by_product[p] for p in record.order_line.product_id if p in order_by_product)

    @api.depends('partner_id.name', 'partner_id.purchase_warn_msg', 'order_line.purchase_line_warn_msg')
    def _compute_purchase_warning_text(self):
        for order in self:
            warnings = OrderedSet()
            if partner_msg := order.partner_id.purchase_warn_msg:
                warnings.add(order.partner_id.name + ' - ' + partner_msg)
            for line in order.order_line:
                if product_msg := line.purchase_line_warn_msg:
                    warnings.add(line.product_id.display_name + ' - ' + product_msg)
            order.purchase_warning_text = '\n'.join(warnings)

    @api.onchange('date_planned')
    def onchange_date_planned(self):
        if self.date_planned:
            self.order_line.filtered(lambda line: not line.display_type).date_planned = self.date_planned

    def _search_is_late(self, operator, value):
        if operator != 'in':
            return NotImplemented
        purchase_domain = [('state', '=', 'purchase'), ('date_planned', '<=', fields.Datetime.now())]
        return [('order_line', 'any', [('order_id', 'any', purchase_domain), ('qty_received', '<', SQL('product_qty'))])]

    @api.model_create_multi
    def create(self, vals_list):
        orders = self.browse()
        for vals in vals_list:
            company_id = vals.get('company_id', self.default_get(['company_id'])['company_id'])
            # Ensures default picking type and currency are taken from the right company.
            self_comp = self.with_company(company_id)
            if vals.get('name', 'New') == 'New':
                seq_date = None
                if 'date_order' in vals:
                    seq_date = fields.Datetime.context_timestamp(self, fields.Datetime.to_datetime(vals['date_order']))
                vals['name'] = self_comp.env['ir.sequence'].next_by_code('purchase.order', sequence_date=seq_date) or '/'
            orders |= super(PurchaseOrder, self_comp).create(vals)
        return orders

    @api.ondelete(at_uninstall=False)
    def _unlink_if_cancelled(self):
        for order in self:
            if not order.state == 'cancel':
                raise UserError(_('In order to delete a purchase order, you must cancel it first.'))

    def copy(self, default=None):
        ctx = dict(self.env.context)
        ctx.pop('default_product_id', None)
        self = self.with_context(ctx)
        new_pos = super().copy(default=default)
        for line in new_pos.order_line:
            if line.product_id:
                seller = line.product_id._select_seller(
                    partner_id=line.partner_id, quantity=line.product_qty,
                    date=line.order_id.date_order and line.order_id.date_order.date(), uom_id=line.product_uom_id)
                line.date_planned = line._get_date_planned(seller)
        return new_pos

    def _must_delete_date_planned(self, field_name):
        # To be overridden
        return field_name == 'order_line'

    def onchange(self, values, field_names, fields_spec):
        """
        Override onchange to NOT update all date_planned on PO lines when
        date_planned on PO is updated by the change of date_planned on PO lines.
        """
        result = super().onchange(values, field_names, fields_spec)
        if any(self._must_delete_date_planned(field) for field in field_names) and 'value' in result:
            for line in result['value'].get('order_line', []):
                if line[0] == Command.UPDATE and 'date_planned' in line[2]:
                    del line[2]['date_planned']
        return result

    def _get_report_base_filename(self):
        self.ensure_one()
        return 'Purchase Order-%s' % (self.name)

    @api.onchange('partner_id', 'company_id')
    def onchange_partner_id(self):
        # Ensures all properties and fiscal positions
        # are taken with the company of the order
        # if not defined, with_company doesn't change anything.
        self = self.with_company(self.company_id)
        default_currency = self._context.get("default_currency_id")
        if not self.partner_id:
            self.fiscal_position_id = False
            self.currency_id = default_currency or self.env.company.currency_id.id
        else:
            self.fiscal_position_id = self.env['account.fiscal.position']._get_fiscal_position(self.partner_id)
            self.payment_term_id = self.partner_id.property_supplier_payment_term_id.id
            self.currency_id = default_currency or self.partner_id.property_purchase_currency_id.id or self.env.company.currency_id.id
            if self.partner_id.buyer_id:
                self.user_id = self.partner_id.buyer_id
        return {}

    @api.onchange('fiscal_position_id', 'company_id')
    def _compute_tax_id(self):
        """
        Trigger the recompute of the taxes if the fiscal position is changed on the PO.
        """
        self.order_line._compute_tax_id()

    # ------------------------------------------------------------
    # MAIL.THREAD
    # ------------------------------------------------------------

    def message_post(self, **kwargs):
        if self.env.context.get('mark_rfq_as_sent'):
            self.filtered(lambda o: o.state == 'draft').write({'state': 'sent'})
            kwargs['notify_author_mention'] = kwargs.get('notify_author_mention', True)
        return super().message_post(**kwargs)

    def _notify_get_recipients_groups(self, message, model_description, msg_vals=False):
        # Tweak 'view document' button for portal customers, calling directly routes for confirm specific to PO model.
        groups = super()._notify_get_recipients_groups(
            message, model_description, msg_vals=msg_vals
        )
        if not self:
            return groups

        self.ensure_one()
        try:
            customer_portal_group = next(group for group in groups if group[0] == 'portal_customer')
        except StopIteration:
            pass
        else:
            access_opt = customer_portal_group[2].setdefault('button_access', {})
            if self.env.context.get('is_reminder'):
                access_opt['title'] = _('View')
            else:
                access_opt['title'] = _('View Quotation') if self.state in ('draft', 'sent') else _('View Order')
                access_opt['url'] = self.get_confirm_url()

        return groups

    def _notify_by_email_prepare_rendering_context(self, message, msg_vals=False, model_description=False,
                                                   force_email_company=False, force_email_lang=False):
        render_context = super()._notify_by_email_prepare_rendering_context(
            message, msg_vals=msg_vals, model_description=model_description,
            force_email_company=force_email_company, force_email_lang=force_email_lang
        )
        subtitles = [render_context['record'].name]
        # don't show price on RFQ mail
        if self.state in ['draft', 'sent']:
            subtitles.append(_('Order\N{NO-BREAK SPACE}due\N{NO-BREAK SPACE}%(date)s',
                date=format_date(self.env, self.date_order, lang_code=render_context.get('lang'))
            ))
        else:
            subtitles.append(format_amount(self.env, self.amount_total, self.currency_id, lang_code=render_context.get('lang')))
        render_context['subtitles'] = subtitles
        return render_context

    def _track_subtype(self, init_values):
        self.ensure_one()
        if 'state' in init_values and self.state == 'purchase':
            if init_values['state'] == 'to approve':
                return self.env.ref('purchase.mt_rfq_approved')
            return self.env.ref('purchase.mt_rfq_confirmed')
        elif 'state' in init_values and self.state == 'to approve':
            return self.env.ref('purchase.mt_rfq_confirmed')
        elif 'state' in init_values and self.state == 'sent':
            return self.env.ref('purchase.mt_rfq_sent')
        return super(PurchaseOrder, self)._track_subtype(init_values)

    # ------------------------------------------------------------
    # ACTIONS
    # ------------------------------------------------------------

    def action_rfq_send(self):
        '''
        This function opens a window to compose an email, with the edi purchase template message loaded by default
        '''
        self.ensure_one()
        ir_model_data = self.env['ir.model.data']
        try:
            if self.env.context.get('send_rfq', False):
                template_id = ir_model_data._xmlid_lookup('purchase.email_template_edi_purchase')[1]
            else:
                template_id = ir_model_data._xmlid_lookup('purchase.email_template_edi_purchase_done')[1]
        except ValueError:
            template_id = False
        try:
            compose_form_id = ir_model_data._xmlid_lookup('mail.email_compose_message_wizard_form')[1]
        except ValueError:
            compose_form_id = False
        ctx = dict(self.env.context or {})
        ctx.update({
            'default_model': 'purchase.order',
            'default_res_ids': self.ids,
            'default_template_id': template_id,
            'default_composition_mode': 'comment',
            'default_email_layout_xmlid': "mail.mail_notification_layout_with_responsible_signature",
            'email_notification_allow_footer': True,
            'force_email': True,
            'hide_mail_template_management_options': True,
            'mark_rfq_as_sent': True,
        })

        # In the case of a RFQ or a PO, we want the "View..." button in line with the state of the
        # object. Therefore, we pass the model description in the context, in the language in which
        # the template is rendered.
        lang = self.env.context.get('lang')
        if {'default_template_id', 'default_model', 'default_res_id'} <= ctx.keys():
            template = self.env['mail.template'].browse(ctx['default_template_id'])
            if template and template.lang:
                lang = template._render_lang([ctx['default_res_id']])[ctx['default_res_id']]

        self = self.with_context(lang=lang)
        if self.state in ['draft', 'sent']:
            ctx['model_description'] = _('Request for Quotation')
        else:
            ctx['model_description'] = _('Purchase Order')

        return {
            'name': _('Compose Email'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(compose_form_id, 'form')],
            'view_id': compose_form_id,
            'target': 'new',
            'context': ctx,
        }

    def action_acknowledge(self):
        self.acknowledged = True

    def action_purchase_comparison(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("purchase.action_purchase_history")
        action['domain'] = [('product_id', 'in', self.order_line.product_id.ids)]
        action['display_name'] = _("Purchase Comparison for %s", self.display_name)
        return action

    def print_quotation(self):
        self.filtered(lambda po: po.state == 'draft').write({'state': "sent"})
        return self.env.ref('purchase.report_purchase_quotation').report_action(self)

    def button_approve(self, force=False):
        self = self.filtered(lambda order: order._approval_allowed())
        self.write({'state': 'purchase', 'date_approve': fields.Datetime.now()})
        self.filtered(lambda p: p.lock_confirmed_po == 'lock').write({'locked': True})
        return {}

    def button_draft(self):
        self.write({'state': 'draft'})
        return {}

    def button_confirm(self):
        for order in self:
            if order.state not in ['draft', 'sent']:
                continue
            order.order_line._validate_analytic_distribution()
            order._add_supplier_to_product()
            # Deal with double validation process
            if order._approval_allowed():
                order.button_approve()
            else:
                order.write({'state': 'to approve'})
        return True

    def button_cancel(self):
        purchase_orders_with_invoices = self.filtered(lambda po: any(i.state not in ('cancel', 'draft') for i in po.invoice_ids))
        if purchase_orders_with_invoices:
            raise UserError(_("Unable to cancel purchase order(s): %s. You must first cancel their related vendor bills.", purchase_orders_with_invoices.mapped('display_name')))
        self.write({'state': 'cancel'})

    def button_lock(self):
        self.locked = True

    def button_unlock(self):
        if self.lock_confirmed_po == 'lock':
            raise UserError(_("Unlocking the order is not allowed as 'Lock Confirmed Orders' is enabled."))
        self.locked = False

    def _prepare_supplier_info(self, partner, line, price, currency):
        # Prepare supplierinfo data when adding a product
        return {
            'partner_id': partner.id,
            'sequence': max(line.product_id.seller_ids.mapped('sequence')) + 1 if line.product_id.seller_ids else 1,
            'min_qty': 1.0,
            'price': price,
            'currency_id': currency.id,
            'discount': line.discount,
            'delay': 0,
        }

    def _add_supplier_to_product(self):
        # Add the partner in the supplier list of the product if the supplier is not registered for
        # this product. We limit to 10 the number of suppliers for a product to avoid the mess that
        # could be caused for some generic products ("Miscellaneous").
        for line in self.order_line:
            # Do not add a contact as a supplier
            partner = self.partner_id if not self.partner_id.parent_id else self.partner_id.parent_id
            already_seller = (partner | self.partner_id) & line.product_id.seller_ids.mapped('partner_id')
            if line.product_id and not already_seller and len(line.product_id.seller_ids) <= 10:
                price = line.price_unit
                # Compute the price for the template's UoM, because the supplier's UoM is related to that UoM.
                if line.product_id.product_tmpl_id.uom_id != line.product_uom_id:
                    default_uom = line.product_id.product_tmpl_id.uom_id
                    price = line.product_uom_id._compute_price(price, default_uom)

                supplierinfo = self._prepare_supplier_info(partner, line, price, line.currency_id)
                # In case the order partner is a contact address, a new supplierinfo is created on
                # the parent company. In this case, we keep the product name and code.
                seller = line.product_id._select_seller(
                    partner_id=line.partner_id,
                    quantity=line.product_qty,
                    date=line.order_id.date_order and line.order_id.date_order.date(),
                    uom_id=line.product_uom_id)
                if seller:
                    supplierinfo['product_name'] = seller.product_name
                    supplierinfo['product_code'] = seller.product_code
                    supplierinfo['product_uom_id'] = line.product_uom.id
                vals = {
                    'seller_ids': [(0, 0, supplierinfo)],
                }
                # supplier info should be added regardless of the user access rights
                line.product_id.product_tmpl_id.sudo().write(vals)

    def action_bill_matching(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Bill Matching"),
            'res_model': 'purchase.bill.line.match',
            'domain': [
                ('partner_id', '=', self.partner_id.id),
                ('company_id', 'in', self.env.company.ids),
                ('purchase_order_id', 'in', [self.id, False]),
            ],
            'views': [(self.env.ref('purchase.purchase_bill_line_match_tree').id, 'list')],
        }

    def _prepare_down_payment_section_values(self):
        self.ensure_one()
        context = {'lang': self.partner_id.lang}
        res = {
            'product_qty': 0.0,
            'order_id': self.id,
            'display_type': 'line_section',
            'is_downpayment': True,
            'sequence': (self.order_line[-1:].sequence or 9) + 1,
            'name': _("Down Payments"),
        }
        del context
        return res

    def _create_downpayments(self, line_vals):
        self.ensure_one()

        # create section
        if not any(line.display_type and line.is_downpayment for line in self.order_line):
            section_line = self.order_line.create(self._prepare_down_payment_section_values())
        else:
            section_line = self.order_line.filtered(lambda line: line.display_type and line.is_downpayment)
        vals = [
            {
                **line_val,
                'sequence': section_line.sequence + i,
            }
            for i, line_val in enumerate(line_vals, start=1)
        ]
        downpayment_lines = self.env['purchase.order.line'].create(vals)
        self.order_line = [
            Command.link(line_id)
            for line_id in downpayment_lines.ids
        ]  # a simple concatenation would cause all order_line to recompute, we do not want it to happen
        return downpayment_lines

    def action_create_invoice(self):
        """Create the invoice associated to the PO.
        """
        precision = self.env['decimal.precision'].precision_get('Product Unit')

        # 1) Prepare invoice vals and clean-up the section lines
        invoice_vals_list = []
        sequence = 10
        for order in self:
            if order.invoice_status != 'to invoice':
                continue

            order = order.with_company(order.company_id)
            pending_section = None
            # Invoice values.
            invoice_vals = order._prepare_invoice()
            # Invoice line values (keep only necessary sections).
            for line in order.order_line:
                if line.display_type == 'line_section':
                    pending_section = line
                    continue
                if not float_is_zero(line.qty_to_invoice, precision_digits=precision):
                    if pending_section:
                        line_vals = pending_section._prepare_account_move_line()
                        line_vals.update({'sequence': sequence})
                        invoice_vals['invoice_line_ids'].append((0, 0, line_vals))
                        sequence += 1
                        pending_section = None
                    line_vals = line._prepare_account_move_line()
                    line_vals.update({'sequence': sequence})
                    invoice_vals['invoice_line_ids'].append((0, 0, line_vals))
                    sequence += 1
            invoice_vals_list.append(invoice_vals)

        if not invoice_vals_list:
            raise UserError(_('There is no invoiceable line. If a product has a control policy based on received quantity, please make sure that a quantity has been received.'))

        # 2) group by (company_id, partner_id, currency_id) for batch creation
        new_invoice_vals_list = []
        for _grouping_keys, invoices in groupby(invoice_vals_list, key=lambda x: (x.get('company_id'), x.get('partner_id'), x.get('currency_id'))):
            origins = set()
            payment_refs = set()
            refs = set()
            ref_invoice_vals = None
            for invoice_vals in invoices:
                if not ref_invoice_vals:
                    ref_invoice_vals = invoice_vals
                else:
                    ref_invoice_vals['invoice_line_ids'] += invoice_vals['invoice_line_ids']
                origins.add(invoice_vals['invoice_origin'])
                payment_refs.add(invoice_vals['payment_reference'])
                refs.add(invoice_vals['ref'])
            ref_invoice_vals.update({
                'ref': ', '.join(refs)[:2000],
                'invoice_origin': ', '.join(origins),
                'payment_reference': len(payment_refs) == 1 and payment_refs.pop() or False,
            })
            new_invoice_vals_list.append(ref_invoice_vals)
        invoice_vals_list = new_invoice_vals_list

        # 3) Create invoices.
        moves = self.env['account.move']
        AccountMove = self.env['account.move'].with_context(default_move_type='in_invoice')
        for vals in invoice_vals_list:
            moves |= AccountMove.with_company(vals['company_id']).create(vals)

        # 4) Some moves might actually be refunds: convert them if the total amount is negative
        # We do this after the moves have been created since we need taxes, etc. to know if the total
        # is actually negative or not
        moves.filtered(lambda m: m.currency_id.round(m.amount_total) < 0).action_switch_move_type()

        return self.action_view_invoice(moves)

    def action_merge(self):
        all_origin = []
        all_vendor_references = []
        rfq_to_merge = self.filtered(lambda r: r.state in ['draft', 'sent'])

        # Group RFQs by vendor
        if len(rfq_to_merge) < 2:
            raise UserError(_("Please select at least two purchase orders with state RFQ and RFQ sent to merge."))

        rfqs_grouped = defaultdict(lambda: self.env['purchase.order'])
        for rfq in rfq_to_merge:
            key = self._prepare_grouped_data(rfq)
            rfqs_grouped[key] += rfq

        bunches_of_rfq_to_be_merge = list(rfqs_grouped.values())
        if all(len(rfq_bunch) == 1 for rfq_bunch in list(bunches_of_rfq_to_be_merge)):
            raise UserError(_("In selected purchase order to merge these details must be same\nVendor, currency, destination, dropship address and agreement"))
        bunches_of_rfq_to_be_merge = [rfqs for rfqs in bunches_of_rfq_to_be_merge if len(rfqs) > 1]

        for rfqs in bunches_of_rfq_to_be_merge:
            if len(rfqs) <= 1:
                continue
            oldest_rfq = min(rfqs, key=lambda r: r.date_order)
            if oldest_rfq:
                # Merge RFQs into the oldest purchase order
                rfqs -= oldest_rfq
                for rfq_line in rfqs.order_line:
                    existing_line = oldest_rfq.order_line.filtered(lambda l: l.product_id == rfq_line.product_id and
                                                                                l.product_uom_id == rfq_line.product_uom_id and
                                                                                l.analytic_distribution == rfq_line.analytic_distribution and
                                                                                l.discount == rfq_line.discount and
                                                                                abs(l.date_planned - rfq_line.date_planned).total_seconds() <= 86400  # 24 hours in seconds
                                                                        )
                    if len(existing_line) > 1:
                        existing_line[0].product_qty += sum(existing_line[1:].mapped('product_qty'))
                        existing_line[1:].unlink()
                        existing_line = existing_line[0]

                    if existing_line:
                        existing_line._merge_po_line(rfq_line)
                    else:
                        rfq_line.order_id = oldest_rfq

                # Merge source documents and vendor references
                all_origin = rfqs.mapped('origin')
                all_vendor_references = rfqs.mapped('partner_ref')

                oldest_rfq.origin = ', '.join(filter(None, [oldest_rfq.origin, *all_origin]))
                oldest_rfq.partner_ref = ', '.join(filter(None, [oldest_rfq.partner_ref, *all_vendor_references]))

                rfq_names = rfqs.mapped('name')
                merged_names = ", ".join(rfq_names)
                oldest_rfq_message = _("RFQ merged with %(oldest_rfq_name)s and %(cancelled_rfq)s", oldest_rfq_name=oldest_rfq.name, cancelled_rfq=merged_names)

                for rfq in rfqs:
                    cancelled_rfq_message = _("RFQ merged with %s", oldest_rfq._get_html_link())
                    rfq.message_post(body=cancelled_rfq_message)
                oldest_rfq.message_post(body=oldest_rfq_message)

                rfqs.filtered(lambda r: r.state != 'cancel').button_cancel()
                oldest_rfq._merge_alternative_po(rfqs)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'message': _('purchase orders merged'),
                'next': {'type': 'ir.actions.act_window_close'},
            }
        }

    def _merge_alternative_po(self, rfqs):
        pass

    def _prepare_grouped_data(self, rfq):
        return (rfq.partner_id.id, rfq.currency_id.id, rfq.dest_address_id.id)

    def _prepare_invoice(self):
        """Prepare the dict of values to create the new invoice for a purchase order.
        """
        self.ensure_one()
        move_type = self._context.get('default_move_type', 'in_invoice')

        partner_invoice = self.env['res.partner'].browse(self.partner_id.address_get(['invoice'])['invoice'])
        partner_bank_id = self.partner_id.commercial_partner_id.bank_ids.filtered_domain(['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)])[:1]

        invoice_vals = {
            'ref': self.partner_ref or '',
            'move_type': move_type,
            'narration': self.notes,
            'currency_id': self.currency_id.id,
            'partner_id': partner_invoice.id,
            'fiscal_position_id': (self.fiscal_position_id or self.fiscal_position_id._get_fiscal_position(partner_invoice)).id,
            'payment_reference': self.partner_ref or '',
            'partner_bank_id': partner_bank_id.id,
            'invoice_origin': self.name,
            'invoice_payment_term_id': self.payment_term_id.id,
            'invoice_line_ids': [],
            'company_id': self.company_id.id,
        }
        return invoice_vals

    def action_view_invoice(self, invoices=False):
        """This function returns an action that display existing vendor bills of
        given purchase order ids. When only one found, show the vendor bill
        immediately.
        """
        if not invoices:
            self.invalidate_model(['invoice_ids'])
            invoices = self.invoice_ids

        result = self.env['ir.actions.act_window']._for_xml_id('account.action_move_in_invoice_type')
        # choose the view_mode accordingly
        if len(invoices) > 1:
            result['domain'] = [('id', 'in', invoices.ids)]
        elif len(invoices) == 1:
            res = self.env.ref('account.view_move_form', False)
            form_view = [(res and res.id or False, 'form')]
            if 'views' in result:
                result['views'] = form_view + [(state, view) for state, view in result['views'] if view != 'form']
            else:
                result['views'] = form_view
            result['res_id'] = invoices.id
        else:
            result = {'type': 'ir.actions.act_window_close'}

        return result

    @api.model
    def retrieve_dashboard(self):
        """ This function returns the values to populate the custom dashboard in
            the purchase order views.
        """
        self.browse().check_access('read')

        result = {
            'global': {
                'draft': {'all': 0, 'priority': 0},
                'sent':  {'all': 0, 'priority': 0},
                'late':  {'all': 0, 'priority': 0},
                'not_acknowledged': {'all': 0, 'priority': 0},
                'late_receipt': {'all': 0, 'priority': 0},
                'days_to_order': 0,
            },
            'my': {
                'draft': {'all': 0, 'priority': 0},
                'sent':  {'all': 0, 'priority': 0},
                'late':  {'all': 0, 'priority': 0},
                'not_acknowledged': {'all': 0, 'priority': 0},
                'late_receipt': {'all': 0, 'priority': 0},
                'days_to_order': 0,
            },
            'days_to_purchase': 0,
        }

        def _update(key, dict_to_update, group):
            for priority, user_id, count in group:
                my = user_id == self.env.user
                dict_to_update['global'][key]['all'] += count
                if priority != '0':
                    dict_to_update['global'][key]['priority'] += count
                if not my:
                    continue
                dict_to_update['my'][key]['all'] += count
                if priority != '0':
                    dict_to_update['my'][key]['priority'] += count

        # easy counts
        groupby = ['priority', 'user_id']
        aggregate = ['id:count_distinct']
        rfq_draft_domain = [('state', '=', 'draft')]
        rfq_draft_group = self.env['purchase.order']._read_group(rfq_draft_domain, groupby, aggregate)
        _update('draft', result, rfq_draft_group)

        rfq_sent_domain = [('state', '=', 'sent')]
        rfq_sent_group = self.env['purchase.order']._read_group(rfq_sent_domain, groupby, aggregate)
        _update('sent', result, rfq_sent_group)

        rfq_late_domain = [('state', 'in', ['draft', 'sent', 'to approve']), ('date_order', '<', fields.Datetime.now())]
        rfq_late_group = self.env['purchase.order']._read_group(rfq_late_domain, groupby, aggregate)
        _update('late', result, rfq_late_group)

        rfq_not_acknowledge = [('state', '=', 'purchase'), ('acknowledged', '=', False)]
        rfq_not_acknowledge_group = self.env['purchase.order']._read_group(rfq_not_acknowledge, groupby, aggregate)
        _update('not_acknowledged', result, rfq_not_acknowledge_group)

        rfq_late_receipt = [('is_late', '=', True)]
        rfq_late_receipt_group = self.env['purchase.order']._read_group(rfq_late_receipt, groupby, aggregate)
        _update('late_receipt', result, rfq_late_receipt_group)

        three_months_ago = fields.Datetime.to_string(fields.Datetime.now() - relativedelta(months=3))

        purchases = self.env['purchase.order'].search_fetch(
            [('state', '=', 'purchase'), ('create_date', '>=', three_months_ago), ('date_approve', '!=', False)],
            ['create_date', 'date_approve', 'user_id'])

        global_deliveries_seconds = 0
        my_deliveries_seconds = 0
        my_deliveries_count = 0

        for po in purchases:
            delivery_seconds = (po.date_approve - po.create_date).total_seconds()
            global_deliveries_seconds += delivery_seconds
            if po.user_id == self.env.user:
                my_deliveries_seconds += delivery_seconds
                my_deliveries_count += 1

        avg_global_deliveries_seconds = global_deliveries_seconds / len(purchases) if purchases else 0
        avg_my_deliveries_seconds = my_deliveries_seconds / my_deliveries_count if my_deliveries_count else 0
        result['global']['days_to_order'] = float_repr(avg_global_deliveries_seconds / 60 / 60 / 24, precision_digits=2)
        result['my']['days_to_order'] = float_repr(avg_my_deliveries_seconds / 60 / 60 / 24, precision_digits=2)

        return result

    def _send_reminder_mail(self, send_single=False):
        if not self.env.user.has_group('purchase.group_send_reminder'):
            return

        template = self.env.ref('purchase.email_template_edi_purchase_reminder', raise_if_not_found=False)
        if template:
            orders = self if send_single else self._get_orders_to_remind()
            for order in orders:
                date = order.date_planned
                if date and (send_single or (date - relativedelta(days=order.reminder_date_before_receipt)).date() == datetime.today().date()):
                    if send_single:
                        return order._send_reminder_open_composer(template.id)
                    else:
                        order.with_context(is_reminder=True).message_post_with_source(
                            template,
                            email_layout_xmlid="mail.mail_notification_layout_with_responsible_signature",
                            subtype_xmlid='mail.mt_comment',
                        )

    def send_reminder_preview(self):
        self.ensure_one()
        if not self.env.user.has_group('purchase.group_send_reminder'):
            return

        template = self.env.ref('purchase.email_template_edi_purchase_reminder', raise_if_not_found=False)
        if template and self.env.user.email and self.id:
            template.with_context(is_reminder=True).send_mail(
                self.id,
                force_send=True,
                raise_exception=False,
                email_layout_xmlid="mail.mail_notification_layout_with_responsible_signature",
                email_values={'email_to': self.env.user.email, 'recipient_ids': []},
            )
            return {'toast_message': escape(_("A sample email has been sent to %s.", self.env.user.email))}

    def _send_reminder_open_composer(self,template_id):
        self.ensure_one()
        try:
            compose_form_id = self.env['ir.model.data']._xmlid_lookup('mail.email_compose_message_wizard_form')[1]
        except ValueError:
            compose_form_id = False
        ctx = dict(self.env.context or {})
        ctx.update({
            'default_model': 'purchase.order',
            'default_res_ids': self.ids,
            'default_template_id': template_id,
            'default_composition_mode': 'comment',
            'default_email_layout_xmlid': "mail.mail_notification_layout_with_responsible_signature",
            'force_email': True,
            'mark_rfq_as_sent': True,
        })
        lang = self.env.context.get('lang')
        if {'default_template_id', 'default_model', 'default_res_id'} <= ctx.keys():
            template = self.env['mail.template'].browse(ctx['default_template_id'])
            if template and template.lang:
                lang = template._render_lang([ctx['default_res_id']])[ctx['default_res_id']]
        self = self.with_context(lang=lang)
        ctx['model_description'] = _('Purchase Order')
        return {
            'name': _('Compose Email'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(compose_form_id, 'form')],
            'view_id': compose_form_id,
            'target': 'new',
            'context': ctx,
        }

    @api.model
    def _get_orders_to_remind(self):
        """When auto sending a reminder mail, only send for unconfirmed purchase
        order and not all products are service."""
        return self.search([
            ('partner_id', '!=', False),
            ('state', '=', 'purchase'),
            ('acknowledged', '=', False),
            ('receipt_reminder_email', '=', True)
        ]).filtered(lambda p: p.mapped('order_line.product_id.product_tmpl_id.type') != ['service'])

    def _default_order_line_values(self, child_field=False):
        default_data = super()._default_order_line_values(child_field)
        new_default_data = self.env['purchase.order.line']._get_product_catalog_lines_data()
        return {**default_data, **new_default_data}

    def action_add_from_catalog(self):
        res = super().action_add_from_catalog()
        if res['context'].get('product_catalog_order_model') == 'purchase.order':
            res['search_view_id'] = [self.env.ref('purchase.product_view_search_catalog').id, 'search']
        kanban_view_id = self.env.ref('purchase.product_view_kanban_catalog_purchase_only').id
        res['views'][0] = (kanban_view_id, 'kanban')
        res['context']['partner_id'] = self.partner_id.id
        return res

    def _get_action_add_from_catalog_extra_context(self):
        return {
            **super()._get_action_add_from_catalog_extra_context(),
            'display_uom': self.env.user.has_group('uom.group_uom'),
            'precision': self.env['decimal.precision'].precision_get('Product Unit'),
            'product_catalog_currency_id': self.currency_id.id,
            'product_catalog_digits': self.order_line._fields['price_unit'].get_digits(self.env),
            'search_default_seller_ids': self.partner_id.name,
        }

    def _get_product_catalog_domain(self):
        return expression.AND([super()._get_product_catalog_domain(), [('purchase_ok', '=', True)]])

    def _get_product_catalog_order_data(self, products, **kwargs):
        res = super()._get_product_catalog_order_data(products, **kwargs)
        for product in products:
            res[product.id] |= self._get_product_price_and_data(product)
        return res

    def _get_product_catalog_record_lines(self, product_ids, **kwargs):
        grouped_lines = defaultdict(lambda: self.env['purchase.order.line'])
        for line in self.order_line:
            if line.display_type or line.product_id.id not in product_ids:
                continue
            grouped_lines[line.product_id] |= line
        return grouped_lines

    def _get_product_price_and_data(self, product):
        """ Fetch the product's data used by the purchase's catalog.

        :return: the product's price and, if applicable, the minimum quantity to
                 buy and the product's packaging data.
        :rtype: dict
        """
        self.ensure_one()
        product_infos = {
            'price': product.standard_price,
            'uom': {
                'display_name': product.uom_id.display_name,
                'id': product.uom_id.id,
            },
        }
        params = {'order_id': self}
        # Check if there is a price and a minimum quantity for the order's vendor.
        seller = product._select_seller(
            partner_id=self.partner_id,
            quantity=None,
            date=self.date_order and self.date_order.date(),
            uom_id=product.uom_id,
            ordered_by='min_qty',
            params=params
        )
        if seller:
            product_infos.update(
                price=seller.price_discounted,
                min_qty=seller.min_qty,
            )

        return product_infos

    def get_acknowledge_url(self):
        return self.get_portal_url(query_string='&acknowledge=True')

    def get_confirm_url(self, confirm_type=None):
        """Create url for confirm reminder or purchase reception email for sending
        in mail. Unsuported anymore. We only use the acknowledge mechanism. Keep it
        for backward compatibility"""
        if confirm_type in ['reminder', 'reception', 'decline']:
            return self.get_acknowledge_url()
        return self.get_portal_url()

    def get_update_url(self):
        """Create portal url for user to update the scheduled date on purchase
        order lines."""
        update_param = url_encode({'update': 'True'})
        return self.get_portal_url(query_string='&%s' % update_param)

    def _approval_allowed(self):
        """Returns whether the order qualifies to be approved by the current user"""
        self.ensure_one()
        return (
            self.company_id.po_double_validation == 'one_step'
            or (self.company_id.po_double_validation == 'two_step'
                and self.amount_total < self.env.company.currency_id._convert(
                    self.company_id.po_double_validation_amount, self.currency_id, self.company_id,
                    self.date_order or fields.Date.today()))
            or self.env.user.has_group('purchase.group_purchase_manager'))

    def get_localized_date_planned(self, date_planned=False):
        """Returns the localized date planned in the timezone of the order's user or the
        company's partner or UTC if none of them are set."""
        self.ensure_one()
        date_planned = date_planned or self.date_planned
        if not date_planned:
            return False
        if isinstance(date_planned, str):
            date_planned = fields.Datetime.from_string(date_planned)
        tz = self.get_order_timezone()
        return date_planned.astimezone(tz)

    def get_order_timezone(self):
        """ Returns the timezone of the order's user or the company's partner
        or UTC if none of them are set. """
        self.ensure_one()
        return timezone(self.user_id.tz or self.company_id.partner_id.tz or 'UTC')

    def _update_date_planned_for_lines(self, updated_dates):
        # create or update the activity
        activity = self.env['mail.activity'].search([
            ('summary', '=', _('Date Updated')),
            ('res_model_id', '=', 'purchase.order'),
            ('res_id', '=', self.id),
            ('user_id', '=', self.user_id.id)], limit=1)
        if activity:
            self._update_update_date_activity(updated_dates, activity)
        else:
            self._create_update_date_activity(updated_dates)

        # update the date on PO line
        for line, date in updated_dates:
            line._update_date_planned(date)

    def _update_order_line_info(self, product_id, quantity, **kwargs):
        """ Update purchase order line information for a given product or create
        a new one if none exists yet.
        :param int product_id: The product, as a `product.product` id.
        :return: The unit price of the product, based on the pricelist of the
                 purchase order and the quantity selected.
        :rtype: float
        """
        self.ensure_one()
        pol = self.order_line.filtered(lambda line: line.product_id.id == product_id)
        if pol:
            if quantity != 0:
                pol.product_qty = quantity
            elif self.state in ['draft', 'sent']:
                price_unit = self._get_product_price_and_data(pol.product_id)['price']
                pol.unlink()
                return price_unit
            else:
                pol.product_qty = 0
        elif quantity > 0:
            pol = self.env['purchase.order.line'].create({
                'order_id': self.id,
                'product_id': product_id,
                'product_qty': quantity,
                'sequence': ((self.order_line and self.order_line[-1].sequence + 1) or 10),  # put it at the end of the order
            })
            seller = pol.product_id._select_seller(
                partner_id=pol.partner_id,
                quantity=pol.product_qty,
                date=pol.order_id.date_order and pol.order_id.date_order.date() or fields.Date.context_today(pol),
                uom_id=pol.product_uom_id)
            if seller:
                # Fix the PO line's price on the seller's one.
                pol.price_unit = seller.price_discounted
        return pol.price_unit_discounted

    def _create_update_date_activity(self, updated_dates):
        note = Markup('<p>%s</p>\n') % _('%s modified receipt dates for the following products:', self.partner_id.name)
        for line, date in updated_dates:
            note += Markup('<p> - %s</p>\n') % _(
                '%(product)s from %(original_receipt_date)s to %(new_receipt_date)s',
                product=line.product_id.display_name,
                original_receipt_date=line.date_planned.date(),
                new_receipt_date=date.date()
            )
        activity = self.activity_schedule(
            'mail.mail_activity_data_warning',
            summary=_("Date Updated"),
            user_id=self.user_id.id
        )
        # add the note after we post the activity because the note can be soon
        # changed when updating the date of the next PO line. So instead of
        # sending a mail with incomplete note, we send one with no note.
        activity.note = note
        return activity

    def _update_update_date_activity(self, updated_dates, activity):
        for line, date in updated_dates:
            activity.note += Markup('<p> - %s</p>\n') %  _(
                '%(product)s from %(original_receipt_date)s to %(new_receipt_date)s',
                product=line.product_id.display_name,
                original_receipt_date=line.date_planned.date(),
                new_receipt_date=date.date()
            )

    def _is_readonly(self):
        """ Return whether the purchase order is read-only or not based on the state.
        A purchase order is considered read-only if its state is 'cancel'.

        :return: Whether the purchase order is read-only or not.
        :rtype: bool
        """
        self.ensure_one()
        return self.state == 'cancel'

    def _get_edi_builders(self):
        return []
