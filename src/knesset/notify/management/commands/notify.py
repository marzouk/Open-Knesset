from django.core.management.base import NoArgsCommand
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.utils.translation import ugettext as _
from django.template.loader import render_to_string
from django.template import TemplateDoesNotExist
from django.conf import settings
import datetime

from actstream.models import Follow, Action
from mailer import send_html_mail
from knesset.mks.models import Member
from knesset.agendas.models import Agenda
from knesset.notify.models import LastSent

class Command(NoArgsCommand):
    help = "Send notification to users via email"

    requires_model_validation = False

#    update_topics = [ # this determines the order in the email. each tuple contains a model class, and the title it will have
#                     (Member,_('Followed MKs')), 
#                     (Agenda,_('Followed Agendas')), 
#                     (None,_('Other'))] # this will contain all actions for other model types
#    update_models = [x[0] for x in update_topics] 
    update_models = [Member,Agenda,None]
    update_topics = []
    for model in update_models:
        try:
            template_name = 'notify/%s_section' % model.__name__.lower()
            update_topics.append( (model, render_to_string(template_name + '.txt'), render_to_string(template_name + '.html')) )
        except TemplateDoesNotExist:
            update_topics.append( (model, model._meta.verbose_name_plural, '<h2>%s</h2>' % model._meta.verbose_name_plural.format()) )
        except AttributeError:
            update_topics.append( (model, _('Other Updates'), _('Other Updates')) )
    try:
        other_models_index = update_models.index(None)
    except ValueError:
        other_models_index = None
    
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'email@example.com')
    days_back = getattr(settings, 'DEFAULT_NOTIFICATION_DAYS_BACK', 10)
    domain = Site.objects.get_current().domain
    
    def handle_noargs(self, **options):
        queued = 0
        for user in User.objects.all():
            user_profile = user.get_profile()
            #if user_profile and user_profile.send_emails: # if this user requested emails
            if user_profile:
                updates = dict(zip(self.update_models, ([] for x in self.update_models))) # will contain the updates to be sent
                updates_html = dict(zip(self.update_models, ([] for x in self.update_models)))
                follows = Follow.objects.filter(user=user) # everything this user is following
                # sometime a user follows something several times. we want to filter that out:
                follows = set([f.actor for f in follows])
                for f in follows:
                    model_class = f.__class__
                    model_template = f.__class__.__name__.lower()
                    model_name = f.__class__._meta.verbose_name
                    content_type = ContentType.objects.get_for_model(f)
                    if model_class in updates:
                        key = model_class
                    else:
                        key = None # put all updates for 'other' classes at the 'None' group
                    try: # get actions that happened since last update
                        last_sent = LastSent.objects.get(user=user, content_type=content_type, object_pk=f.id)
                        last_sent_time = last_sent.time
                        stream = Action.objects.filter(actor_content_type = content_type,
                                                       actor_object_id = f.id,
                                                       timestamp__gt=last_sent_time,
                                                       ).order_by('-timestamp')
                        last_sent.save() # update timestamp
                    except LastSent.DoesNotExist: # never updates about this actor, send some updates 
                        stream = Action.objects.filter(actor_content_type = content_type,
                                                       actor_object_id = f.id,
                                                       timestamp__gt=datetime.datetime.now()-datetime.timedelta(self.days_back),
                                                       ).order_by('-timestamp')
                        last_sent = LastSent.objects.create(user=user,content_type=content_type, object_pk=f.id)
                    if stream: # this actor has some updates
                        try: # genereate the appropriate header for this actor class
                            header = render_to_string(('notify/%(model)s_header.txt' % {'model': model_template}),{'model':model_name,'object':f})
                        except TemplateDoesNotExist:
                            header = render_to_string(('notify/model_header.txt'),{'model':model_name,'object':f})
                        try:
                            header_html = render_to_string(('notify/%(model)s_header.html' % {'model': model_template}),{'model':model_name,'object':f,'domain':self.domain})
                        except TemplateDoesNotExist:                            
                            header_html = render_to_string(('notify/model_header.html'),{'model':model_name,'object':f,'domain':self.domain})
                        updates[key].append(header)
                        updates_html[key].append(header_html)
                        
                    for action_instance in stream: # now generate the updates themselves
                        try:                            
                            action_output = render_to_string(('activity/%(verb)s/action_email.txt' % { 'verb':action_instance.verb.replace(' ','_') }),{ 'action':action_instance },None)
                        except TemplateDoesNotExist: # fallback to the generic template
                            action_output = render_to_string(('activity/action_email.txt'),{ 'action':action_instance },None)
                        try:
                            action_output_html = render_to_string(('activity/%(verb)s/action_email.html' % { 'verb':action_instance.verb.replace(' ','_') }),{ 'action':action_instance,'domain':self.domain },None)
                        except TemplateDoesNotExist: # fallback to the generic template
                            action_output_html = render_to_string(('activity/action_email.html'),{ 'action':action_instance,'domain':self.domain },None)
                        updates[key].append(action_output)
                        updates_html[key].append(action_output_html)
                        
            email_body = []
            email_body_html = []
            for (model_class,title,title_html) in self.update_topics:
                if updates[model_class]: # this model has some updates, add it to the email
                    email_body.append(title.format())
                    email_body.append('\n'.join(updates[model_class]))
                    email_body_html.append(title_html.format())
                    email_body_html.append(''.join(updates_html[model_class]))
            if email_body: # there are some updates. generate email
                header = render_to_string(('notify/header.txt'),{ 'user':user })
                footer = render_to_string(('notify/footer.txt'),{ 'user':user,'domain':self.domain })
                header_html = render_to_string(('notify/header.html'),{ 'user':user })
                footer_html = render_to_string(('notify/footer.html'),{ 'user':user,'domain':self.domain })
                send_html_mail(_('Open Knesset Updates'), "%s\n%s\n%s" % (header, '\n'.join(email_body), footer), 
                                                          "%s\n%s\n%s" % (header_html, ''.join(email_body_html), footer_html),
                                                          self.from_email,
                                                          [user.email],
                                                          )
                queued += 1
                
        print "%d email notifications queued for sending" % queued
